#!/usr/bin/env python3
"""Живой замер: работают ли темы (треды) в ЛИЧНОМ диалоге с ботом (#1463).

Зачем скрипт, а не чтение документации. Документация Bot API 9.2/10.0 говорит
две вещи, которые не складываются в ответ:

  * у объекта `User` появилось поле `has_topics_enabled` — «forum topic mode
    enabled for the bot in private chats», то есть темы в личке существуют;
  * в списке методов, получивших поддержку приватных чатов, перечислены
    `editForumTopic`, `deleteForumTopic`, `unpinAllForumTopicMessages` —
    и НЕ перечислен `createForumTopic`.

Плюс висит неразобранный отчёт tdlib/telegram-bot-api#847: после 10.0
`sendMessage` с `message_thread_id` в приватный чат отвечает
`400 Bad Request: message thread not found`. Прочитать это и пересказать —
ровно то, что AGENTS.md называет «не подтверждено, выданное за факт». Ответ
даёт только вызов: создать тему, написать в неё, переименовать, удалить.

## Что именно доказывает этот прогон

Каждый шаг печатает НАСТОЯЩИЙ ответ Bot API (`ok`, `error_code`,
`description`) — не пересказ. Различаются три исхода, и различаются они
данными, а не догадкой (AGENTS.md, «Алерт не гадает»):

  themes_absent   `createForumTopic` отказал → тем в личке нет вовсе (или
                  нужен переключатель, и API называет какой);
  send_broken     тема создалась, но `sendMessage(message_thread_id=…)`
                  отказал → ровно случай #847, темы есть, доставка в них нет;
  works           тема создана, сообщение в неё доставлено, тема удалена.

Контрольный шаг (обычный `sendMessage` без темы) стоит ПЕРВЫМ намеренно:
без него «всё отказало» не отличается от «токен не тот / чат не тот», и
вывод был бы про темы там, где на самом деле про доступ.

## Секреты

Токен уходит в путь URL и не печатается никогда. Ответы Bot API несут
`chat.id` — это значение секрета `TELEGRAM_CHAT_ID`; GitHub маскирует точное
совпадение, но полагаться на это одно нельзя (маскируется ТОЛЬКО точное
совпадение — производные нет), поэтому `redact` вычищает известные значения
из всего, что печатается, своими руками.

## Уборка

Созданная тема удаляется в `finally`: замер не оставляет мусора в чате
владельца. Если удаление не прошло — это печатается явно, с id темы, чтобы
остаток было видно, а не «наверное, удалилось».

Запуск (нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в окружении):
    python scripts/telegram/thread_probe.py
Тесты:
    python -m pytest scripts/telegram/test_thread_probe.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import sys
import urllib.error
import urllib.request

API_ROOT = "https://api.telegram.org"
TIMEOUT_SECONDS = 20

# Исходы замера. Строки — часть контракта: их читает и docs/research, и
# будущий резолвер темы, поэтому они названы здесь, а не собираются на месте.
OUTCOME_WORKS = "works"
OUTCOME_SEND_BROKEN = "send_broken"
OUTCOME_THEMES_ABSENT = "themes_absent"
OUTCOME_NO_ACCESS = "no_access"

# Имя пробной темы. Видно владельцу в чате несколько секунд, поэтому оно
# говорит, что происходит, а не «test».
PROBE_TOPIC_NAME = "🔬 замер тредов (#1463)"
PROBE_TOPIC_RENAMED = "🔬 замер тредов — переименование"


class Redactor:
    """Вычищает значения секретов из печатаемого текста.

    Отдельный класс, а не функция с глобалью: тест обязан уметь построить
    редактор со своими значениями, не трогая окружение процесса."""

    def __init__(self, secrets: list[str]):
        # Короткие значения не редактируем: подстрока из двух символов
        # вырезала бы половину осмысленного текста, и лог стал бы нечитаем.
        self._secrets = sorted((s for s in secrets if s and len(s) >= 4), key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<секрет>")
        return text


def call(token: str, method: str, payload: dict) -> dict:
    """Вызов метода Bot API. Возвращает РАЗОБРАННОЕ тело ответа.

    Ошибка Bot API — это HTTP 400/403 с осмысленным JSON в теле, и именно это
    тело здесь и нужно: `description` — единственное место, где Telegram
    говорит, чего не хватает. `urllib` на 4xx кидает HTTPError, из которого
    тело ещё надо достать — поэтому except, а не голый вызов.

    Сетевой отказ (DNS, таймаут) — другой класс: не ответ API, а отсутствие
    ответа. Он возвращается отдельным признаком `transport`, чтобы вывод не
    выдал «Telegram отказал» там, где Telegram вообще не ответил."""
    request = urllib.request.Request(
        f"{API_ROOT}/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "error_code": error.code, "description": body[:500]}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"ok": False, "transport": True, "description": f"сеть: {error}"}


def describe(result: dict) -> str:
    """Однострочный человекочитаемый итог вызова."""
    if result.get("ok"):
        return "ok"
    code = result.get("error_code", "—")
    return f"отказ {code}: {result.get('description', '')}"


class Probe:
    """Шаги замера. Класс, чтобы протокол (список шагов с их ответами)
    собирался в одном месте и печатался целиком, а не по кускам."""

    def __init__(self, token: str, chat_id: str, redact: Redactor,
                 caller=call, out=print):
        self._token = token
        self._chat_id = chat_id
        self._redact = redact
        self._call = caller
        self._out = out
        self.protocol: list[tuple[str, dict]] = []

    def step(self, title: str, method: str, payload: dict) -> dict:
        result = self._call(self._token, method, dict(payload, chat_id=self._chat_id)
                            if method != "getMe" else payload)
        self.protocol.append((title, result))
        self._out(f"  {title}: {self._redact(describe(result))}")
        return result

    def dump(self, title: str, result: dict) -> None:
        """Полное тело ответа — для шагов, где важен не только ok/описание."""
        self._out(f"  {title} (тело): "
                  + self._redact(json.dumps(result, ensure_ascii=False, sort_keys=True)))


def run(token: str, chat_id: str, redact: Redactor, caller=call, out=print) -> dict:
    """Исполняет замер и возвращает вердикт: {outcome, topic_id, why}.

    Возвращает словарь, а не печатает вердикт сам: вызывающий кладёт его в
    итог прогона (job summary) машиночитаемым, а печать — отдельная забота."""
    probe = Probe(token, chat_id, redact, caller=caller, out=out)

    out("Шаг 0. Кто мы и включены ли темы у бота (getMe)")
    me = probe.step("getMe", "getMe", {})
    probe.dump("getMe", me)
    has_topics = me.get("result", {}).get("has_topics_enabled")
    out(f"  has_topics_enabled = {has_topics!r} "
        f"({'поле отсутствует — версия API старше 9.2 или режим не включён' if has_topics is None else 'поле есть'})")

    out("Шаг 1. Контроль: обычное сообщение без темы")
    control = probe.step("sendMessage без темы", "sendMessage",
                         {"text": "🔬 замер тредов (#1463): контрольное сообщение"})
    if not control.get("ok"):
        return {"outcome": OUTCOME_NO_ACCESS, "topic_id": None,
                "why": f"контрольная отправка не прошла — дело не в темах: {describe(control)}"}

    out("Шаг 2. Создание темы в личке (createForumTopic)")
    created = probe.step("createForumTopic", "createForumTopic", {"name": PROBE_TOPIC_NAME})
    probe.dump("createForumTopic", created)
    if not created.get("ok"):
        return {"outcome": OUTCOME_THEMES_ABSENT, "topic_id": None,
                "why": f"создать тему не дали: {describe(created)}"}

    topic_id = created.get("result", {}).get("message_thread_id")
    out(f"  message_thread_id = {topic_id!r}")

    try:
        out("Шаг 3. Отправка В тему (sendMessage + message_thread_id) — это и есть #847")
        sent = probe.step("sendMessage в тему", "sendMessage",
                          {"text": "🔬 сообщение внутри темы", "message_thread_id": topic_id})
        if not sent.get("ok"):
            return {"outcome": OUTCOME_SEND_BROKEN, "topic_id": topic_id,
                    "why": f"тема создана, но доставка в неё отказала: {describe(sent)}"}

        out("Шаг 4. Переименование темы (editForumTopic)")
        probe.step("editForumTopic", "editForumTopic",
                   {"message_thread_id": topic_id, "name": PROBE_TOPIC_RENAMED})

        out("Шаг 5. Снятие закреплений (unpinAllForumTopicMessages)")
        probe.step("unpinAllForumTopicMessages", "unpinAllForumTopicMessages",
                   {"message_thread_id": topic_id})

        return {"outcome": OUTCOME_WORKS, "topic_id": topic_id,
                "why": "тема создана, сообщение в неё доставлено, правка темы принята"}
    finally:
        out("Шаг 6. Уборка: удаление пробной темы (deleteForumTopic)")
        removed = probe.step("deleteForumTopic", "deleteForumTopic",
                             {"message_thread_id": topic_id})
        if not removed.get("ok"):
            out(f"::warning::замер оставил тему {topic_id} — удалить руками: {describe(removed)}")


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        # Fail loud: «возможности нет» и «возможность есть, но сломана» —
        # разные сообщения, потому что лечатся по-разному (AGENTS.md).
        print("::error::thread_probe: нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID в окружении — "
              "замер не выполнялся (это не «темы не работают», это «не запускали»)")
        return 2

    redact = Redactor([token, chat_id])
    verdict = run(token, chat_id, redact)

    print()
    print(f"ВЕРДИКТ ЗАМЕРА: {verdict['outcome']}")
    print(f"ПОЧЕМУ: {redact(verdict['why'])}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(f"## Замер тредов в личке (#1463)\n\n")
            summary.write(f"* исход: `{verdict['outcome']}`\n")
            summary.write(f"* почему: {redact(verdict['why'])}\n")

    # Отказ Telegram — это РЕЗУЛЬТАТ замера, а не провал прогона: красный
    # шаг на честно измеренном «темы не работают» заставил бы следующего
    # агента чинить исправный код. rc 0 на любом установленном исходе,
    # rc 1 — только когда исход установить не удалось.
    return 0 if verdict["outcome"] != OUTCOME_NO_ACCESS else 1


if __name__ == "__main__":
    sys.exit(main())
