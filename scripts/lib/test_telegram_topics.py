#!/usr/bin/env python3
"""Стенд слоя тем Telegram (#1461 + #1465).

Стенд поднимает НАСТОЯЩИЙ `http.server` и зовёт настоящие функции модуля;
недоступность воспроизводится ЗАКРЫТЫМ ПОРТОМ, а запись на data-ветку — в
настоящем git-репозитории с `git init --bare` в роли origin. Заглушка вместо
сервера — это пересказ чужого формата, и она ломается на исправном коде
(AGENTS.md, живой случай 2026-09-19, #1373/PR #1380).

Тела ответов Bot API взяты в прод-форме из дословного вывода замера #1463
(`docs/research/26-telegram-bot-api-threads.md`), а не придуманы здесь.

Запуск: python -m pytest scripts/lib/test_telegram_topics.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import ast
import http.server
import json
import re
import socket
import subprocess
import threading
import urllib.request
import sys

import pytest

_spec = importlib.util.spec_from_file_location(
    "telegram_topics", Path(__file__).resolve().parent / "telegram_topics.py")
tt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tt)


# ── Настоящий сервер вместо заглушки ────────────────────────────────────────

class _Stand:
    """Маленький живой HTTP-сервер: отдаёт то, что ему положили, и ведёт
    журнал реально полученных запросов — по нему тесты проверяют, что вызова
    НЕ БЫЛО, а не только что результат совпал."""

    def __init__(self):
        self.routes: dict[str, tuple[int, str]] = {}
        self.calls: list[tuple[str, dict]] = []
        stand = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):  # тишина в выводе pytest
                pass

            def _answer(self):
                status, body = stand.routes.get(self.path, (404, '{"ok": false}'))
                payload = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                stand.calls.append((self.path, {}))
                self._answer()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                try:
                    params = json.loads(raw)
                except json.JSONDecodeError:
                    params = {"<нераспарсено>": raw}
                stand.calls.append((self.path, params))
                self._answer()

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def methods_called(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for path, _ in self.calls]

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def stand():
    server = _Stand()
    yield server
    server.close()


def _closed_port() -> int:
    """Порт, который точно никто не слушает: открыли и сразу закрыли."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _opener_to(base: str):
    """Переписывает адрес запроса на наш живой сервер, СОХРАНЯЯ путь и тело:
    модуль собирает настоящий `urllib.request.Request`, и стенд проверяет
    именно то, что он собрал."""
    def opener(request, timeout=None):
        path = request.full_url.split("://", 1)[1].split("/", 1)[1]
        moved = urllib.request.Request(f"{base}/{path}", data=request.data,
                                       headers=dict(request.header_items()),
                                       method=request.get_method())
        return urllib.request.urlopen(moved, timeout=timeout)
    return opener


# ── Слой 1: реестр категорий ────────────────────────────────────────────────

def test_registry_titles_fit_bot_api_limit():
    """Заголовок длиннее предела Bot API отказал бы при создании темы — и
    узнали бы мы об этом в проде, а не здесь."""
    for category, title in tt.CATEGORIES.items():
        assert title.strip(), f"пустой заголовок у категории {category}"
        assert len(title) <= tt.TOPIC_TITLE_MAX_CHARS, f"{category}: {len(title)} символов"


def test_registry_has_no_duplicate_titles():
    """Две категории с одинаковым заголовком неотличимы в чате владельца."""
    titles = list(tt.CATEGORIES.values())
    assert len(titles) == len(set(titles)), titles


def test_unknown_category_is_a_loud_refusal_naming_known_ones():
    """Умолчания «всё остальное» нет: оно и есть то состояние, из которого
    уходим. Сообщение обязано назвать известные — иначе автор сигнала гадает."""
    with pytest.raises(KeyError) as caught:
        tt.category_title("что-то-своё")
    assert "decision" in str(caught.value)
    assert "breakage" in str(caught.value)


# ── Разбор и сборка карты ───────────────────────────────────────────────────

def test_parse_topics_reads_prod_form():
    text = json.dumps({"schema": 1, "topics": {
        "decision": {"message_thread_id": 42, "title": "🟣 Решения владельца", "created_ts": 1790000000},
        "breakage": {"message_thread_id": 77, "title": "🔴 Поломки", "created_ts": 1790000001},
    }}, ensure_ascii=False)
    assert tt.parse_topics(text) == {"decision": 42, "breakage": 77}


@pytest.mark.parametrize("text", ["", "не json", "[]", '{"topics": 5}', '{"topics": {"a": 1}}'])
def test_broken_map_is_an_empty_map_not_a_crash(text):
    """Карта восстановима (темы создадутся заново), сигнал владельцу — нет."""
    assert tt.parse_topics(text) == {}


def test_forgotten_topic_reads_as_absent_not_as_thread_zero():
    """`forget_topic` затирает исчезнувшую тему нулём. Ноль, прочитанный как
    рабочий id, отправил бы сигнал в несуществующую тему — и так по кругу."""
    text = json.dumps({"topics": {"infra": {"message_thread_id": 0, "title": "x", "created_ts": 1}}})
    assert tt.parse_topics(text) == {}


def test_render_keeps_other_categories_written_by_a_parallel_job():
    current = json.dumps({"schema": 1, "topics": {
        "breakage": {"message_thread_id": 77, "title": "🔴 Поломки", "created_ts": 1}}},
        ensure_ascii=False)
    updated = tt.render_topics(current, "decision", 42, "🟣 Решения владельца", 2)
    assert tt.parse_topics(updated) == {"breakage": 77, "decision": 42}


# ── Чтение карты по HTTP ────────────────────────────────────────────────────

def test_fetch_reads_live_server(stand):
    path = f"/{tt.DATA_BRANCH}/{tt.TOPICS_PATH}"
    stand.routes[f"/mytab0r/edge-harness{path}"] = (200, json.dumps(
        {"schema": 1, "topics": {"pipeline": {"message_thread_id": 9, "title": "t", "created_ts": 1}}}))
    assert tt.fetch_topics("mytab0r/edge-harness", _opener_to(stand.base())) == {"pipeline": 9}


def test_missing_map_is_not_a_failure(stand):
    """404 значит «тем ещё не заводили», а не «сломалось»: лечится созданием
    темы, а не расследованием."""
    assert tt.fetch_topics("mytab0r/edge-harness", _opener_to(stand.base())) == {}


def test_unreachable_host_falls_back_to_empty_map_and_says_so(capsys):
    """Недоступность — ЗАКРЫТЫЙ ПОРТ, а не подправленная заглушка."""
    base = f"http://127.0.0.1:{_closed_port()}"
    assert tt.fetch_topics("mytab0r/edge-harness", _opener_to(base)) == {}
    assert "::warning::" in capsys.readouterr().err


# ── Создание темы ───────────────────────────────────────────────────────────

# Прод-форма ответа createForumTopic — дословно из замера #1463.
_CREATED_OK = json.dumps({"ok": True, "result": {
    "message_thread_id": 137, "name": "🟣 Решения владельца", "icon_color": 7322096}})


def test_create_topic_returns_thread_id_from_live_answer(stand):
    stand.routes["/botTOKEN/createForumTopic"] = (200, _CREATED_OK)
    assert tt.create_topic("TOKEN", "-100123", "🟣 Решения владельца", _opener_to(stand.base())) == 137
    assert stand.methods_called() == ["createForumTopic"]
    assert stand.calls[0][1]["name"] == "🟣 Решения владельца"


def test_refusal_carries_telegram_reason(stand):
    """«Чат не форум» и «у бота нет прав» лечатся по-разному — причина обязана
    дойти до читателя (AGENTS.md, «Алерт не гадает»)."""
    stand.routes["/botTOKEN/createForumTopic"] = (400, json.dumps(
        {"ok": False, "error_code": 400,
         "description": "Bad Request: the chat is not a forum"}))
    with pytest.raises(tt.TopicUnavailable) as caught:
        tt.create_topic("TOKEN", "-100123", "т", _opener_to(stand.base()))
    assert "not a forum" in str(caught.value)


def test_ok_without_thread_id_is_a_contract_violation(stand):
    """`ok: true` с пустым результатом — то же нарушение контракта, что и
    явный отказ, и молча пройти оно не должно."""
    stand.routes["/botTOKEN/createForumTopic"] = (200, json.dumps({"ok": True, "result": {}}))
    with pytest.raises(tt.TopicUnavailable):
        tt.create_topic("TOKEN", "-100123", "т", _opener_to(stand.base()))


def test_token_never_appears_in_the_refusal_text(stand):
    """Репозиторий публичный, а маскируется только точное совпадение секрета —
    производное (в тексте исключения, в логе job'а) не маскируется."""
    # Форма настоящего токена Bot API (<id>:<строка>), значение синтетическое.
    secret = "123456:AAHsecretTokenValueNeverInLogs"
    stand.routes[f"/bot{secret}/createForumTopic"] = (
        400, json.dumps({"ok": False, "description": "Bad Request: not enough rights"}))
    with pytest.raises(tt.TopicUnavailable) as caught:
        tt.create_topic(secret, "-100123", "t", _opener_to(stand.base()))
    assert "not enough rights" in str(caught.value), "причина отказа потерялась"
    assert secret not in str(caught.value)
    assert "AAHsecretTokenValueNeverInLogs" not in str(caught.value)


# ── Разрешение категории в тему ─────────────────────────────────────────────

def test_known_category_does_not_create_a_second_topic(stand):
    """Ради этого всё и делается: тема создаётся ОДИН раз. Проверяется по
    журналу реальных вызовов сервера, а не по совпадению возвращённого id."""
    stand.routes[f"/mytab0r/edge-harness/{tt.DATA_BRANCH}/{tt.TOPICS_PATH}"] = (200, json.dumps(
        {"schema": 1, "topics": {"decision": {"message_thread_id": 42, "title": "т", "created_ts": 1}}}))
    got = tt.resolve_thread_id("decision", "mytab0r/edge-harness", "TOKEN", "-100123", 5,
                               _opener_to(stand.base()))
    assert got == 42
    assert "createForumTopic" not in stand.methods_called()


def test_unknown_category_refuses_before_touching_the_network(stand):
    with pytest.raises(KeyError):
        tt.resolve_thread_id("нет-такой", "mytab0r/edge-harness", "TOKEN", "-100123", 5,
                             _opener_to(stand.base()))
    assert stand.calls == []


# ── Исчезнувшая тема ────────────────────────────────────────────────────────

@pytest.mark.parametrize("description", [
    "Bad Request: message thread not found",
    "Bad Request: TOPIC_DELETED",
    "Bad Request: thread not found",
])
def test_gone_topic_is_recognised_by_description(description):
    """Машинного кода на этот случай Bot API не даёт — только текст."""
    assert tt.topic_is_gone(description)


@pytest.mark.parametrize("description", [
    None, "", "Bad Request: chat not found", "Too Many Requests: retry after 12",
])
def test_other_failures_are_not_mistaken_for_a_gone_topic(description):
    """Иначе на каждом 429 мы заводили бы новую тему."""
    assert not tt.topic_is_gone(description)


# ── Запись на data-ветку: настоящий git ─────────────────────────────────────

def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", check=True)


@pytest.fixture
def origin(tmp_path):
    """Настоящий bare-репозиторий в роли origin: `clone_data_branch` и
    `append_and_push` зовутся как в проде, включая ветку, которой ещё нет."""
    path = tmp_path / "origin.git"
    path.mkdir()
    _git("init", "--bare", "--initial-branch=main", ".", cwd=path)
    # У origin обязан быть коммит на main: `clone_data_branch` заводит ещё не
    # существующую data-ветку ОТ main (так же, как в проде) — пустой bare без
    # main дал бы отказ стенда на исправном коде.
    seed = tmp_path / "seed"
    _git("init", "--initial-branch=main", str(seed), cwd=tmp_path)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed", cwd=seed)
    _git("push", "--quiet", str(path), "main", cwd=seed)
    return path


def test_first_write_creates_the_branch_and_the_second_is_a_noop(origin, tmp_path):
    first = tt.persist_topic("ignored/repo", "decision", 42, "🟣 Решения владельца", 100,
                             workdir=str(tmp_path / "w1"))
    assert first is True, "первая запись не состоялась"

    listing = _git("ls-tree", "-r", "--name-only", tt.DATA_BRANCH, cwd=origin).stdout
    assert tt.TOPICS_PATH in listing

    blob = _git("show", f"{tt.DATA_BRANCH}:{tt.TOPICS_PATH}", cwd=origin).stdout
    assert tt.parse_topics(blob) == {"decision": 42}

    second = tt.persist_topic("ignored/repo", "decision", 42, "🟣 Решения владельца", 200,
                              workdir=str(tmp_path / "w2"))
    assert second is False, "та же пара записана повторно — лишний коммит на каждый прогон"


def test_second_category_does_not_erase_the_first(origin, tmp_path):
    tt.persist_topic("ignored/repo", "decision", 42, "🟣 Решения владельца", 100,
                     workdir=str(tmp_path / "w1"))
    tt.persist_topic("ignored/repo", "breakage", 77, "🔴 Поломки", 101,
                     workdir=str(tmp_path / "w2"))
    blob = _git("show", f"{tt.DATA_BRANCH}:{tt.TOPICS_PATH}", cwd=origin).stdout
    assert tt.parse_topics(blob) == {"decision": 42, "breakage": 77}


def test_forgetting_a_topic_makes_the_next_resolve_create_it_again(origin, tmp_path):
    """Тему удалили руками — сигнал не теряется, тема заводится заново."""
    tt.persist_topic("ignored/repo", "pipeline", 55, "⚙️ Конвейер", 100,
                     workdir=str(tmp_path / "w1"))
    tt.forget_topic("ignored/repo", "pipeline", 101)
    blob = _git("show", f"{tt.DATA_BRANCH}:{tt.TOPICS_PATH}", cwd=origin).stdout
    assert "pipeline" not in tt.parse_topics(blob), "забытая тема всё ещё читается как рабочая"
    assert '"pipeline"' in blob, "запись исчезла целиком — не видно, что категория известна"


@pytest.fixture(autouse=True)
def _origin_env(origin, monkeypatch):
    """Подменяется ровно одно — адрес origin. Клонирование, коммит и push
    остаются настоящими."""
    monkeypatch.setattr(tt.data_branch_writer, "origin_url", lambda *a, **k: str(origin))
    real_clone = tt.data_branch_writer.clone_data_branch
    monkeypatch.setattr(tt.data_branch_writer, "clone_data_branch",
                        lambda _origin, workdir, branch: real_clone(str(origin), workdir, branch))


# ── Гвардия: реестр категорий против фактических отправителей ───────────────

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


#: Функции, вызов которых = отправка сигнала владельцу. `escalate` попал сюда
#: не сразу, и это стоило живого случая (#1490): первая редакция гвардии
#: разбирала ТОЛЬКО `send_telegram`, а `escalate` — один такой вызов, и
#: категорию он называл. Проверялась не та граница: настоящие отправители —
#: сорок вызывающих `escalate`, а не он сам. Владелец увидел в теме «Решения
#: владельца» алерт о квоте и сообщение предохранителя конвейера.
SENDER_FUNCTIONS = ("send_telegram", "escalate")


def _sender_calls() -> list[tuple[str, int, ast.Call]]:
    """Все вызовы отправителей сигнала в scripts/, кроме самих тестов.

    Разбор AST, а не grep: `grep 'category='` прошёл бы и на упоминании в
    комментарии, и на переносе строки внутри вызова. Учитываются обе формы —
    прямая (`escalate(...)`) и через атрибут (`pulse_guard.escalate(...)`):
    половина вызывающих пользуется второй, и пропустить её значило бы
    повторить ту же ошибку границы."""
    import ast as _ast
    found = []
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = _ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in SENDER_FUNCTIONS:
                found.append((str(path.relative_to(REPO_ROOT)), node.lineno, node))
    return found


def _forwarding_functions() -> set[tuple[str, int]]:
    """(файл, строка вызова) тех вызовов, что ПРОБРАСЫВАЮТ категорию своего
    вызывающего, а не называют её сами.

    Такой вызов существует ровно один — `escalate` передаёт категорию дальше в
    `send_telegram`. Разрешается он не по имени функции, а по двум условиям
    сразу: значение аргумента — голое имя `category`, И объемлющая функция
    сама требует `category` как keyword-only БЕЗ умолчания. Второе условие
    обязательно: проброс из функции с умолчанием — это та же зашитая
    категория, только через переменную, то есть ровно дефект #1490 в обход
    гвардии."""
    import ast as _ast
    allowed: set[tuple[str, int]] = set()
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        rel = str(path.relative_to(REPO_ROOT))
        tree = _ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for func in _ast.walk(tree):
            if not isinstance(func, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            names = [a.arg for a in func.args.kwonlyargs]
            if "category" not in names:
                continue
            if func.args.kw_defaults[names.index("category")] is not None:
                continue  # есть умолчание — проброс не считается
            for node in _ast.walk(func):
                if not isinstance(node, _ast.Call):
                    continue
                inner = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if inner not in SENDER_FUNCTIONS:
                    continue
                kw = next((k for k in node.keywords if k.arg == "category"), None)
                if isinstance(kw.value if kw else None, _ast.Name) and kw.value.id == "category":
                    allowed.add((rel, node.lineno))
    return allowed


def test_every_sender_names_a_known_category():
    """Сигнал без категории — это возврат к одному потоку, а узнать об этом из
    прода дороже, чем из CI. Проверяется ФАКТ вызова, не наличие подстроки."""
    calls = _sender_calls()
    assert len(calls) > 30, (
        f"найдено всего {len(calls)} вызовов отправителей — гвардия смотрит не туда; "
        "на момент #1490 их было сорок с лишним")
    forwarding = _forwarding_functions()
    problems = []
    for rel, line, node in calls:
        if (rel, line) in forwarding:
            continue  # проброс категории вызывающего — проверяется у него
        keyword = next((k for k in node.keywords if k.arg == "category"), None)
        if keyword is None:
            problems.append(f"{rel}:{line}: вызов без category=")
            continue
        if not isinstance(keyword.value, ast.Constant) or not isinstance(keyword.value.value, str):
            problems.append(f"{rel}:{line}: category= не строковый литерал — реестр не сверить")
            continue
        if keyword.value.value not in tt.CATEGORIES:
            problems.append(f"{rel}:{line}: категория {keyword.value.value!r} вне реестра")
    assert not problems, "\n".join(problems)


def test_every_registered_category_is_actually_used():
    """Категория, которой никто не шлёт, — это тема-пустышка в чате владельца:
    расхождение реестра и набора тем в другую сторону."""
    used = set()
    for _, _, node in _sender_calls():
        keyword = next((k for k in node.keywords if k.arg == "category"), None)
        if keyword is not None and isinstance(keyword.value, ast.Constant):
            used.add(keyword.value.value)
    unused = sorted(set(tt.CATEGORIES) - used)
    assert not unused, f"в реестре есть, но никто не шлёт: {unused}"


# ── Второй отправитель — bash: проверяется исполнением, не чтением ──────────

TASK_SH = REPO_ROOT / "scripts" / "worker" / "task.sh"


def _run_telegram_report(*args: str, extra_env: dict | None = None):
    """Вырезает НАСТОЯЩЕЕ тело `telegram_report` из task.sh и исполняет его на
    заглушке транспорта. Текстовый разбор исходника здесь не годится: вырезанная
    проверка при сохранённом тексте прошла бы его молча (AGENTS.md,
    «Поведенческий тест находит то, чего структурный не видит»)."""
    import os
    import shutil
    if shutil.which("bash") is None:
        pytest.skip("bash недоступен")
    body = subprocess.run(
        ["awk", "/^telegram_report\\(\\) \\{/,/^\\}/", str(TASK_SH)],
        capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert "canary_http" in body, "тело telegram_report не вырезалось — гвардия смотрит не туда"
    script = (
        "set -uo pipefail\n"
        f'SCRIPT_DIR="{TASK_SH.parent}"\n'
        'canary_http() { echo "SENT $*" >>"$SEND_LOG"; }\n'
        + body + "\n"
        'telegram_report ' + " ".join(f'"{a}"' for a in args) + "\n"
        'echo "RC=$?"\n'
    )
    import tempfile
    log = Path(tempfile.mkdtemp(prefix="telegram-report-")) / "sent.log"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42", "SEND_LOG": str(log)}
    env.update(extra_env or {})
    # `>/dev/null` в самом вызове глушит stdout заглушки — журнал пишется в
    # файл, иначе стенд краснел бы на исправном коде (поймано исполнением).
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            encoding="utf-8", env=env)
    result.sent = log.read_text(encoding="utf-8") if log.exists() else ""
    return result


def test_bash_sender_refuses_without_a_category():
    """Без категории bash-отправитель обязан отказать громко, а не тихо
    отправить в общий поток: тихая отправка и есть прежнее состояние."""
    result = _run_telegram_report("текст")
    assert "::error::" in result.stdout + result.stderr, result.stdout + result.stderr
    assert result.sent == "", "сигнал ушёл без категории"


def test_bash_sender_sends_when_a_category_is_given():
    """Положительная сторона того же пути: с категорией отправка происходит.
    Темы в этом прогоне нет (нет GITHUB_REPOSITORY) — сигнал уходит в общий
    поток, и это штатно, а не отказ."""
    result = _run_telegram_report("текст", "pipeline")
    assert "SENT" in result.sent, result.stdout + result.stderr
    assert "message_thread_id" not in result.sent, "темы нет — не должно быть и аргумента"


def test_every_bash_sender_names_a_known_category():
    """Разбор bash AST'ом невозможен — здесь разбор строки вызова. Носитель
    поведения при этом выше: сам `telegram_report` отказывает без категории,
    это проверено исполнением."""
    problems = []
    for number, line in enumerate(TASK_SH.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("telegram_report ") or "() {" in stripped:
            continue
        if not any(f'"{category}"' in stripped for category in tt.CATEGORIES):
            problems.append(f"scripts/worker/task.sh:{number}: категория вне реестра")
    assert problems == [] or not problems, "\n".join(problems)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ── Вторая сторона реестра: воркер (#1495) ─────────────────────────────────
#
# Гвардия выше разбирает `scripts/**/*.py`. Воркер — TypeScript в `cf-worker/`,
# и для неё его отправителей не существовало: четыре `sendMessage` уходили
# владельцу вообще без `message_thread_id`, а CI был зелёным. Тот же дефект
# границы, что #1490, слоем выше — там проверялось одно имя функции вместо
# роли, здесь один язык вместо всех мест, откуда сообщение физически уходит.

CONFIG_TS = REPO_ROOT / "cf-worker" / "src" / "config.ts"
HARNESS_TS = REPO_ROOT / "cf-worker" / "src" / "harness.ts"

#: Единственный санкционированный отправитель воркера. Всё остальное, что
#: зовёт sendMessage, обязано идти через него — иначе появляется второй путь
#: к владельцу, и тему он не знает.
WORKER_SENDER = "#alertOwner"


def _ts_method_body(source: str, name: str) -> str:
    """Тело метода TS от открывающей `{` до парной закрывающей — счётом
    скобок, а не жадным regex: тело само содержит `{`/`}`. Тот же приём, что
    в scripts/lib/test_do_hotpath_aggregate_guard.py."""
    match = re.search(rf"{re.escape(name)}\s*\([^)]*\)\s*:[^{{]*\{{", source)
    assert match, f"метод {name} не найден в harness.ts — переименован без правки гвардии?"
    start = source.index("{", match.end() - 1)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"не нашёл закрывающую скобку метода {name}")


def _ts_topic_titles() -> dict[str, str]:
    """`TELEGRAM.topicTitles` из config.ts как словарь. Импортировать .ts в
    Python нельзя, поэтому читаем исходник текстом — ровно тем же способом,
    что scripts/lib/test_telegram_callback_format_sync.py уже читает
    callbackPrefix (#254/#486)."""
    source = CONFIG_TS.read_text(encoding="utf-8")
    match = re.search(r"topicTitles:\s*\{(.*?)\}\s*as\s+Record", source, re.S)
    assert match, "в config.ts не найден литерал TELEGRAM.topicTitles"
    return dict(re.findall(r'(\w+)\s*:\s*"([^"]+)"', match.group(1)))


def test_topic_titles_match_between_python_and_typescript():
    """Заголовки тем совпадают на обоих языках — иначе две темы одной
    категории.

    Тема ищется и заводится ПО ЗАГОЛОВКУ: у воркера нет доступа к ветке
    `data/telegram-topics`, где живёт Python-ская карта, и единственный общий
    источник правды у двух сторон — сам Telegram. Значит расходиться нельзя
    именно заголовкам: разошлись — в чате владельца появятся «🔴 Поломки» от
    Python и «🔴 Поломки » от воркера как ДВЕ РАЗНЫЕ темы, и ни один тест
    каждой стороны по отдельности этого не увидит (тот же класс, что #486).

    Сверяется реестр ЦЕЛИКОМ, а не та категория, что нужна воркеру: «совпало
    по тому, что я взял» неотличимо от «совпало»."""
    assert _ts_topic_titles() == dict(tt.CATEGORIES), (
        "реестр категорий разошёлся между cf-worker/src/config.ts "
        "(TELEGRAM.topicTitles) и scripts/lib/telegram_topics.py (CATEGORIES): "
        f"TS={_ts_topic_titles()} vs Python={dict(tt.CATEGORIES)}"
    )


def test_worker_alert_category_is_known_and_not_the_decision_topic():
    """Категория алертов воркера существует в реестре и это НЕ тема решений:
    под алертами воркера кнопок нет и быть не может, а тема решений по
    определению содержит только то, под чем кнопки есть (#1490)."""
    source = CONFIG_TS.read_text(encoding="utf-8")
    match = re.search(r'workerAlertCategory:\s*"([^"]+)"', source)
    assert match, "в config.ts не найдена TELEGRAM.workerAlertCategory"
    category = match.group(1)
    assert category in tt.CATEGORIES, (
        f"категория алертов воркера «{category}» отсутствует в реестре "
        f"({sorted(tt.CATEGORIES)})"
    )
    assert category != tt.DECISION_CATEGORY, (
        "алерты воркера уходят в тему решений владельца, а кнопок под ними нет — "
        "ровно то, что владелец увидел в #1490"
    )


def test_worker_sends_to_owner_only_through_the_single_door():
    """Все `sendMessage` воркера живут внутри `#alertOwner` — двери, которая
    знает про темы.

    Это и был дефект #1495: четыре вызова стояли прямо в телах
    `#tickStorageReadyAlert`/`#tickPulseAlert` и уходили без
    `message_thread_id`. Проверяется ЧИСЛО вызовов в файле против числа
    вызовов внутри двери: новый пятый вызов где угодно ещё покраснеет, даже
    если он выглядит правильным."""
    source = HARNESS_TS.read_text(encoding="utf-8")
    everywhere = source.count('"sendMessage"')
    inside = _ts_method_body(source, WORKER_SENDER).count('"sendMessage"')
    assert inside > 0, (
        f"{WORKER_SENDER} не зовёт sendMessage — дверь переименована или выпотрошена, "
        "а гвардия этого не заметила бы (класс #891: структурная проверка по имени)"
    )
    assert everywhere == inside, (
        f"в harness.ts {everywhere} вызовов sendMessage, а внутри {WORKER_SENDER} — {inside}: "
        f"лишние уходят владельцу мимо тем (#1495). Зови {WORKER_SENDER}(text), "
        "он сам резолвит тему и честно помечает сообщение, если темы не досталось"
    )


def test_worker_door_names_the_reason_when_the_topic_is_missing():
    """Тормоз назвал газ: не досталось темы — сообщение всё равно уходит, но с
    причиной. Потерять алерт дороже, чем показать его не там (ADR 0028), а
    молча показать не там — silent-wrong, который читатель не отличит от
    исправной работы."""
    body = _ts_method_body(HARNESS_TS.read_text(encoding="utf-8"), WORKER_SENDER)
    assert "в общем потоке" in body, (
        f"{WORKER_SENDER} не помечает сообщение, ушедшее мимо темы — владелец не отличит "
        "«тема не досталась» от «так и задумано» (#1495)"
    )
