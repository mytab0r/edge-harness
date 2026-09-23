#!/usr/bin/env python3
"""Категории сигналов владельцу и их темы в Telegram (#1461 + #1465).

Класс одной фразой: **все сигналы владельцу — решение, поломка, шум
конвейера, инфраструктура — валятся в один поток, и решение тонет в шуме.**

Здесь единственное место правды на ДВА слоя сразу, потому что порознь они
бессмысленны: категория без темы — это префикс в тексте, тема без категории —
некуда маршрутизировать.

  1. РЕЕСТР КАТЕГОРИЙ (`CATEGORIES`) — машинный id, заголовок темы и цвет
     иконки. Отправитель обязан назвать категорию явно: умолчания нет, потому
     что молчаливое «всё остальное» и есть то состояние, из которого уходим.
  2. СООТВЕТСТВИЕ `категория → message_thread_id` — тема создаётся один раз,
     её id обязан пережить прогон job'а.

## Где живёт соответствие и почему не в морде

Носитель — data-ветка `data/telegram-topics`, файл `topics.json`; тот же приём,
что у `data/worktree-cleanup` и `data/pipeline-health`, и та же библиотека
записи (`data_branch_writer`, #882) с ретраем на гонку параллельных job'ов.

Рассмотрены и отвергнуты (реестр вариантов задачи #1465):

* **Durable Object.** Отправители живут в GitHub Actions и ходили бы в морду
  за id темы на каждый сигнал — то есть канал владельца стал бы зависеть от
  живости морды. Ровно этот отказ случился 2026-09-22/23 (#1477): морда
  отвечала Telegram'у 4xx, канал встал, и починить это владелец мог только
  через тот же канал. Заводить эту зависимость ещё раз — значит повторить
  инцидент.
* **Закреплённое сообщение в чате.** Хранилище внутри самого Telegram,
  внешних зависимостей нет — но слот закрепления ОДИН и принадлежит
  владельцу: он закрепит что-нибудь своё, и соответствие исчезнет молча.

## Чтение не тратит бюджет GitHub API

Репозиторий публичный, поэтому штатное чтение идёт с
`raw.githubusercontent.com` — без токена и без счётчика (живой бюджет
installation-токена мы исчерпывали трижды за сутки, 2026-09-23). У raw есть
кэш порядка минут, поэтому ПЕРЕД созданием темы (редкий путь: четыре раза за
всё время) карта перечитывается авторитетно через API — иначе два
одновременных job'а завели бы две темы одной категории.

## Чего здесь нет

Отправки. Её делают вызывающие (`pulse_guard.send_telegram`,
`scripts/worker/task.sh`) — этот модуль только отвечает на вопрос «в какую
тему». Отказ ответить — это `None`, и он обязан означать «шлём в общий поток с
явной пометкой», а не «не шлём»: потерять сигнал дороже, чем показать его не
в той теме.

Запуск тестов: python -m pytest scripts/lib/test_telegram_topics.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

_data_branch_spec = importlib.util.spec_from_file_location(
    "data_branch_writer", Path(__file__).resolve().parent / "data_branch_writer.py")
data_branch_writer = importlib.util.module_from_spec(_data_branch_spec)
_data_branch_spec.loader.exec_module(data_branch_writer)


# ── Слой 1: реестр категорий ────────────────────────────────────────────────

#: Машинный id → заголовок темы. Единственное место правды: и отправители, и
#: гвардия расхождения реестра с набором тем читают отсюда.
#: Заголовок темы Bot API ограничивает 128 символами — проверяется гвардией.
#: `infra` в первой редакции реестра была и удалена: отправителя у неё нет ни
#: одного, а категория без отправителя — это пустая тема в чате владельца, то
#: же расхождение реестра и набора тем, только в другую сторону. Нашла это не
#: проза, а гвардия (`test_every_registered_category_is_actually_used`) на
#: первом же прогоне. Появится сигнал об инфраструктуре — строка вернётся
#: вместе с ним, одним изменением.
CATEGORIES: dict[str, str] = {
    "decision": "🟣 Решения владельца",
    "breakage": "🔴 Поломки",
    "pipeline": "⚙️ Конвейер",
}

#: Предел Bot API на `name` в createForumTopic — свойство чужой системы,
#: сверять с нашим выбором нечего, поэтому константа, а не настройка.
TOPIC_TITLE_MAX_CHARS = 128

DATA_BRANCH = "data/telegram-topics"
TOPICS_PATH = "topics.json"
SCHEMA_VERSION = 1

#: Чат владельца (TELEGRAM_CHAT_ID) — СЕКРЕТ, репозиторий публичный: в
#: `topics.json` он не попадает ни в каком виде (AGENTS.md, «Секреты»). Цена
#: решения названа вслух: карта не знает, какому чату принадлежит, и после
#: смены чата хранит чужие id. Это не тихая ошибка — отправка в несуществующую
#: тему отказывает, и `forget_topic` заводит тему заново (см. ниже).
TIMEOUT_SECONDS = 20


class TopicUnavailable(RuntimeError):
    """Тему получить не удалось. Несёт причину ТЕКСТОМ для пометки в сигнале:
    читателю важно, почему сообщение пришло в общий поток, а не в тему."""


def category_title(category: str) -> str:
    """Заголовок темы по категории. Неизвестная категория — громкий отказ, а
    не «свалим в общий поток»: молчаливое «всё остальное» и есть дефект."""
    try:
        return CATEGORIES[category]
    except KeyError:
        known = ", ".join(sorted(CATEGORIES))
        raise KeyError(f"неизвестная категория сигнала {category!r}; известные: {known}") from None


# ── Слой 2: соответствие категория → message_thread_id ──────────────────────

def raw_topics_url(repo: str) -> str:
    """Публичный адрес карты. Репозиторий публичный — токен не нужен, счётчик
    installation-токена не тратится."""
    return f"https://raw.githubusercontent.com/{repo}/{DATA_BRANCH}/{TOPICS_PATH}"


def parse_topics(text: str) -> dict[str, int]:
    """Разбор `topics.json` в плоское `категория → message_thread_id`.

    Битое содержимое — НЕ повод падать: карта восстановима (темы создадутся
    заново), а сигнал владельцу — нет. Возвращаем пустую карту, вызывающий
    увидит это как «темы нет» и заведёт её."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    topics = parsed.get("topics")
    if not isinstance(topics, dict):
        return {}
    result: dict[str, int] = {}
    for category, record in topics.items():
        if not isinstance(record, dict):
            continue
        thread_id = record.get("message_thread_id")
        # `> 0` намеренно: `forget_topic` затирает исчезнувшую тему нулём, и
        # ноль обязан читаться как «темы нет», а не как рабочий id. Запись при
        # этом остаётся в файле — видно, что категория известна и тема была.
        if isinstance(thread_id, int) and not isinstance(thread_id, bool) and thread_id > 0:
            result[category] = thread_id
    return result


def render_topics(current: str, category: str, thread_id: int, title: str, now_ts: int) -> str:
    """Новое содержимое `topics.json` с добавленной/заменённой записью.
    Читает ТЕКУЩЕЕ содержимое и дописывает в него — чужие категории, которые
    параллельный job успел записать, не теряются."""
    try:
        parsed = json.loads(current) if current.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    topics = parsed.get("topics")
    if not isinstance(topics, dict):
        topics = {}
    topics[category] = {"message_thread_id": thread_id, "title": title, "created_ts": now_ts}
    parsed["schema"] = SCHEMA_VERSION
    parsed["topics"] = topics
    return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def fetch_topics(repo: str, opener=urllib.request.urlopen) -> dict[str, int]:
    """Штатное чтение карты. Ветки/файла ещё нет (404) — это НЕ отказ, а
    «тем пока не заводили»: пустая карта. Сеть недоступна — тоже пустая карта
    с предупреждением: хуже пустой карты только несделанная отправка."""
    url = raw_topics_url(repo)
    request = urllib.request.Request(url, headers={"User-Agent": "edge-harness-telegram-topics"})
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return parse_topics(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        print(f"::warning::карта тем не прочитана (HTTP {error.code}) — сигнал уйдёт в общий поток",
              file=sys.stderr)
        return {}
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        print(f"::warning::карта тем не прочитана ({error}) — сигнал уйдёт в общий поток",
              file=sys.stderr)
        return {}


def bot_api(token: str, method: str, params: dict, opener=urllib.request.urlopen) -> dict:
    """Вызов Bot API. Отдаёт разобранный ответ ЦЕЛИКОМ (и ok, и description) —
    вызывающему нужна причина отказа, а не факт «не получилось».

    Секрет в текст исключения не подставляется нигде в этом модуле: токен
    живёт только в URL запроса, а сообщения об ошибке строятся из `description`
    Telegram и кода HTTP (AGENTS.md, «Секреты»: маскируется только точное
    совпадение, производное — нет)."""
    data = json.dumps(params).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "edge-harness-telegram-topics"},
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {}
        parsed.setdefault("ok", False)
        parsed.setdefault("description", f"HTTP {error.code}")
        return parsed
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return {"ok": False, "description": f"транспорт: {error}"}


def _authoritative_topics(repo: str) -> dict[str, int]:
    """Перечитать карту через API, минуя кэш raw. Зовётся ТОЛЬКО перед
    созданием темы (четыре раза за всё время) — на штатном пути бюджет
    installation-токена не тратится."""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{TOPICS_PATH}?ref={DATA_BRANCH}",
         "--jq", ".content"],
        capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        # Ветки/файла нет, нет прав, исчерпан бюджет — все три означают
        # «авторитетно подтвердить нечем». Возвращаем пустую карту: хуже
        # лишней темы только отсутствие темы вообще.
        return {}
    import base64
    try:
        return parse_topics(base64.b64decode(result.stdout.strip()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def create_topic(token: str, chat_id: str, title: str, opener=urllib.request.urlopen) -> int:
    """Завести тему и вернуть её `message_thread_id`.

    Чат не форум, бот без прав `can_manage_topics`, сеть — всё это одна и та
    же новость для вызывающего («темы не будет»), но причина РАЗНАЯ и уходит
    в текст исключения: их и лечат по-разному (AGENTS.md, «Алерт не гадает»)."""
    answer = bot_api(token, "createForumTopic", {"chat_id": chat_id, "name": title}, opener)
    if not answer.get("ok"):
        raise TopicUnavailable(
            f"createForumTopic отказал: {answer.get('description') or '<без описания>'}")
    thread_id = (answer.get("result") or {}).get("message_thread_id")
    if not isinstance(thread_id, int) or isinstance(thread_id, bool):
        raise TopicUnavailable(
            f"createForumTopic ответил ok, но без message_thread_id: {answer.get('result')!r}")
    return thread_id


def persist_topic(repo: str, category: str, thread_id: int, title: str, now_ts: int,
                  workdir: str | None = None) -> bool:
    """Записать пару в `topics.json` на data-ветке. True — записали, False —
    там уже лежала та же пара (гонку выиграл другой job)."""
    # Адрес origin — у единственного места правды (#1486). Своя копия здесь
    # была и убрана: она вшивала токен прямо в URL, а тот попадает в
    # `.git/config` клона и в текст ошибок git (git печатает remote при
    # отказе). Репозиторий публичный, и GitHub маскирует в логах только
    # ТОЧНОЕ совпадение со значением секрета — производное не маскируется
    # (AGENTS.md, «Секреты»). Аутентификацию ставит `gh auth setup-git`
    # (credential helper), как у соседних писателей data-веток.
    del repo  # адрес берётся из GITHUB_REPOSITORY тем же способом, что у соседей
    origin = data_branch_writer.origin_url()
    temp = workdir or tempfile.mkdtemp(prefix="telegram-topics-")
    data_branch_writer.clone_data_branch(origin, temp, DATA_BRANCH)

    def is_duplicate(current: str) -> bool:
        return parse_topics(current).get(category) == thread_id

    return data_branch_writer.append_and_push(
        workdir=temp,
        data_branch=DATA_BRANCH,
        rel_path=TOPICS_PATH,
        commit_identity=("-c", "user.name=edge-harness", "-c", "user.email=noreply@github.com"),
        commit_message=f"telegram-topics: {category} -> {thread_id}",
        is_duplicate=is_duplicate,
        render_next=lambda current: render_topics(current, category, thread_id, title, now_ts),
    )


def resolve_thread_id(category: str, repo: str, token: str, chat_id: str, now_ts: int,
                      opener=urllib.request.urlopen) -> int:
    """`категория → message_thread_id`, заводя тему при первом обращении.

    Отказ — исключение `TopicUnavailable` с ПРИЧИНОЙ: вызывающий обязан
    отправить сигнал в общий поток и назвать эту причину в тексте, а не
    промолчать и не потерять сообщение."""
    title = category_title(category)
    known = fetch_topics(repo, opener)
    if category in known:
        return known[category]

    # Редкий путь. Кэш raw живёт минутами, и два одновременных job'а успели бы
    # завести по теме на категорию — перед созданием перечитываем авторитетно.
    known = _authoritative_topics(repo)
    if category in known:
        return known[category]

    thread_id = create_topic(token, chat_id, title, opener)
    try:
        persist_topic(repo, category, thread_id, title, now_ts)
    except RuntimeError as error:
        # Тема СОЗДАНА, но не записана: следующий прогон заведёт вторую.
        # Это видимый мусор, а не потеря сигнала, поэтому громко предупреждаем
        # и отдаём рабочий id, а не падаем.
        print(f"::warning::тема {category} создана ({thread_id}), но не записана в "
              f"{DATA_BRANCH} — следующий прогон заведёт ещё одну: {error}", file=sys.stderr)
    return thread_id


def forget_topic(repo: str, category: str, now_ts: int) -> None:
    """Забыть тему, которой больше нет (удалили руками). Запись затирается
    id = 0 — карта остаётся полной (видно, что категория известна), а
    `parse_topics` такой id не отдаёт как рабочий, поэтому следующее
    обращение заведёт тему заново."""
    try:
        persist_topic(repo, category, 0, category_title(category), now_ts)
    except RuntimeError as error:
        print(f"::warning::не удалось забыть исчезнувшую тему {category}: {error}", file=sys.stderr)


#: Описания Telegram, означающие «этой темы больше нет». Проверяется
#: подстрокой в нижнем регистре: Bot API не даёт машинного кода на этот
#: случай, только текст `description` (проверено замером #1463).
TOPIC_GONE_MARKERS = (
    "thread not found",
    "topic_deleted",
    "topic deleted",
    "message thread not found",
)


def topic_is_gone(description: str | None) -> bool:
    """Отказ отправки означает «темы больше нет», а не «сломалась отправка»."""
    text = (description or "").lower()
    return any(marker in text for marker in TOPIC_GONE_MARKERS)


def _cli(argv: list[str]) -> int:
    """`resolve <категория>` — напечатать message_thread_id в stdout, ничего
    не напечатать при отказе. Это вход для bash-отправителя
    (`scripts/worker/task.sh`): вторая копия логики тем в bash была бы ровно
    тем «одним местом правды в двух экземплярах», которого правило не терпит.

    Код возврата ВСЕГДА 0: «темы нет» — не повод не отправить сигнал. Причина
    уходит в stderr `::warning::`, вызывающий шлёт в общий поток."""
    if len(argv) != 2 or argv[0] != "resolve":
        print("использование: telegram_topics.py resolve <категория>", file=sys.stderr)
        for key, value in CATEGORIES.items():
            print(f"{key}\t{value}", file=sys.stderr)
        return 2
    category = argv[1]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (repo and token and chat):
        print("::warning::GITHUB_REPOSITORY/TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы "
              "— тему определить нечем, сигнал уйдёт в общий поток", file=sys.stderr)
        return 0
    try:
        print(resolve_thread_id(category, repo, token, chat, int(time.time())))
    except (TopicUnavailable, KeyError) as error:
        print(f"::warning::тема не получена ({error}) — сигнал уйдёт в общий поток",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
