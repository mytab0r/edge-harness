#!/usr/bin/env python3
"""Регистрация вебхука Telegram (issue #490): setWebhook + проверка
видимого результата через getWebhookInfo, а не одного «ok: true».

Порядок относительно #254 (PR #486): маршрут /api/messages/ingest обязан
УЖЕ принимать секретный заголовок X-Telegram-Bot-Api-Secret-Token раньше,
чем вебхук зарегистрирован — иначе Telegram начнёт слать апдейты в морду,
которая их отвергает, и это осядет в last_error_message. Вместо того чтобы
полагаться на память «сначала слить PR, потом запускать workflow», скрипт
сам зондирует маршрут (probe_route) синтетическим POST без побочных
эффектов (см. её докстрок) и решает по факту, готова ли морда.

Идемпотентность: если getWebhookInfo уже показывает нужный url без
last_error_message, setWebhook не вызывается повторно.

drop_pending_updates сознательно НЕ выставляется: апдейты, накопленные до
первой регистрации, могут быть директивами владельца в инбокс (#20) —
Telegram доставит их через новый вебхук (идемпотентность по update_id в
морде не даст задвоить), а drop_pending_updates=true потерял бы их молча.

Секреты никогда не печатаются целиком: в лог попадают только статусы HTTP,
имена полей Telegram-ответа и текст ошибки Telegram (description) — сам
TELEGRAM_BOT_TOKEN/TELEGRAM_WEBHOOK_SECRET в текст исключений не
подставляется нигде в этом файле.

Заголовок User-Agent (issue #524): по умолчанию `urllib.request` отправляет
`Python-urllib/<версия>` — Cloudflare перед `{HARNESS_URL}` (воркер живёт на
shared-зоне `*.workers.dev`, своих правил WAF там нет, см.
docs/research/20-cloudflare-free.md) отвечает на этот конкретный User-Agent
403 `error code: 1010` (Browser Integrity Check) ДО того, как запрос доходит
до воркера — измерено живым прогоном из GitHub Actions: curl тем же секретом
с того же job получает штатный 400 need_source_msg_id, тот же urllib с тем же
секретом без переопределения User-Agent — 403 1010, тот же urllib с любым
неблокируемым User-Agent — снова 400. Поэтому здесь свой честный UA, а не
имитация браузера.

Запуск:  python scripts/telegram/register_webhook.py
Тесты:   python -m pytest scripts/telegram/test_register_webhook.py -q
"""

from __future__ import annotations

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

WEBHOOK_PATH = "/api/messages/ingest"
TIMEOUT = 15
# См. докстрок модуля, issue #524: значение по умолчанию у urllib.request
# ловит Cloudflare error 1010 перед *.workers.dev — здесь единственное место
# правды, читают все вызовы _request через дефолт headers.
USER_AGENT = "edge-harness-telegram-webhook/1 (+https://github.com/mytab0r/edge-harness)"


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> tuple[int, dict | None]:
    """Возвращает (http_status, json_или_None). URL строит вызывающий —
    здесь он не логируется и не появляется в тексте исключений."""
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, method=method, headers=merged_headers, data=data)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read()
        status = error.code
    parsed = None
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
    return status, parsed


def probe_route(harness_url: str, secret: str) -> str:
    """'ready' — маршрут принял секретный заголовок и дошёл до разбора тела:
    пустой объект `{}` без побочных эффектов гарантированно даёт 400
    need_source_msg_id (см. #postMessageIngest в cf-worker/src/harness.ts —
    ошибка бросается ДО первой записи в БД). 'not_ready' — 401 unauthorized:
    морда так отвечает и на несуществующий маршрут (PR #486 не слит), и на
    маршрут с разошедшимся секретом — оба случая означают «не регистрируем
    вебхук сейчас», различать их не обязательно, оба требуют человека.
    Что угодно ещё — 'unexpected:<status>', неожиданный ответ, разбираться
    вручную, а не гадать.

    Форма тела ошибки (issue #524, находка живого прогона) — `ApiError` в
    cf-worker/src/harness.ts всегда отдаёт `{"error": {"code": …, "message": …}}`,
    вложенный объект, а не плоскую строку `{"error": "need_source_msg_id"}` —
    сверяется именно `error.code`."""
    status, parsed = _request(
        f"{harness_url}{WEBHOOK_PATH}",
        method="POST",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": secret,
            "Content-Type": "application/json",
        },
        data=b"{}",
    )
    error = parsed.get("error") if isinstance(parsed, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if status == 400 and code == "need_source_msg_id":
        return "ready"
    if status == 401:
        return "not_ready"
    return f"unexpected:{status}"


def set_webhook(bot_token: str, harness_url: str, secret: str) -> None:
    payload = json.dumps(
        {
            "url": f"{harness_url}{WEBHOOK_PATH}",
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query"],
        }
    ).encode()
    status, parsed = _request(
        f"https://api.telegram.org/bot{bot_token}/setWebhook",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=payload,
    )
    if status != 200 or not isinstance(parsed, dict) or not parsed.get("ok"):
        description = parsed.get("description") if isinstance(parsed, dict) else None
        raise RuntimeError(
            f"setWebhook отклонён Telegram (HTTP {status}): {description or '<без описания>'}"
        )


def get_webhook_info(bot_token: str) -> dict:
    status, parsed = _request(f"https://api.telegram.org/bot{bot_token}/getWebhookInfo")
    if status != 200 or not isinstance(parsed, dict) or not parsed.get("ok"):
        raise RuntimeError(f"getWebhookInfo не ответил (HTTP {status})")
    result = parsed.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("getWebhookInfo.result отсутствует в ответе Telegram")
    return result


def already_registered(info: dict, expected_url: str) -> bool:
    return info.get("url") == expected_url and not info.get("last_error_message")


def verify(info: dict, expected_url: str) -> None:
    """Видимый результат (issue #490, п.2): проверяем ПОЛЯ getWebhookInfo,
    не факт, что setWebhook вернул ok: true."""
    if info.get("url") != expected_url:
        raise RuntimeError(
            f"getWebhookInfo.url не совпадает с ожидаемым адресом (см. HARNESS_URL): {info.get('url')!r}"
        )
    if "pending_update_count" not in info:
        raise RuntimeError("getWebhookInfo не вернул pending_update_count")
    last_error = info.get("last_error_message")
    if last_error:
        raise RuntimeError(f"getWebhookInfo.last_error_message не пуст: {last_error}")


def main() -> int:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    harness_url = os.environ.get("HARNESS_URL")
    # true — ручной workflow_dispatch: владелец явно попросил регистрацию
    # сейчас, «не готово» обязано остановить job громко. false — автозапуск
    # после deploy-worker: «не готово» значит «PR #486 ещё не слит», это
    # ожидаемое переходное состояние, не авария (см. докстрок probe_route).
    fail_on_not_ready = os.environ.get("FAIL_ON_NOT_READY", "true").strip().lower() != "false"

    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", bot_token),
            ("TELEGRAM_WEBHOOK_SECRET", secret),
            ("HARNESS_URL", harness_url),
        )
        if not value
    ]
    if missing:
        message = f"Не задано в окружении: {', '.join(missing)} — регистрация невозможна."
        if fail_on_not_ready:
            print(f"::error::{message}", file=sys.stderr)
            return 1
        print(f"::warning::{message}", file=sys.stderr)
        return 0

    assert bot_token and secret and harness_url  # для mypy/читателя — проверено выше
    expected_url = f"{harness_url}{WEBHOOK_PATH}"

    readiness = probe_route(harness_url, secret)
    if readiness == "not_ready":
        message = (
            "Маршрут /api/messages/ingest ответил 401 на верный секретный заголовок — "
            "либо PR #486 (#254) ещё не слит и не задеплоен, либо TELEGRAM_WEBHOOK_SECRET "
            "разошёлся между репозиторием и воркером (сверь secrets репозитория и вывод "
            "'Секреты воркера' в deploy-worker). Регистрация вебхука пропущена."
        )
        if fail_on_not_ready:
            print(f"::error::{message}", file=sys.stderr)
            return 1
        print(f"::warning::{message}", file=sys.stderr)
        return 0
    if readiness != "ready":
        print(
            f"::error::Зонд маршрута вернул неожиданный статус ({readiness}) — регистрация остановлена.",
            file=sys.stderr,
        )
        return 1

    info = get_webhook_info(bot_token)
    if already_registered(info, expected_url):
        print(
            "Вебхук уже указывает на нужный адрес и last_error_message пуст — "
            "регистрация не требуется (идемпотентно)."
        )
        return 0

    set_webhook(bot_token, harness_url, secret)
    info = get_webhook_info(bot_token)
    verify(info, expected_url)
    print(
        "Вебхук зарегистрирован: url совпадает с HARNESS_URL, "
        f"pending_update_count={info.get('pending_update_count')}, last_error_message пуст."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
