#!/usr/bin/env python3
"""Перехват отвергнутого запроса dsh пересылающим рекордером (#1525).

Зонд #1520 исчерпал перебор: OpenRouter отвечает МОДЕЛЬЮ на том же URL и с тем
же ключом, а `dsh` на этой же записи получает
`INVALID_REQUEST: Invalid Anthropic Messages API request`. Ни потолок вывода,
ни внепротокольные поля, ни форма целиком, ни объём до 2 МБ отказа не
воспроизвели. Дальше перебирать нечего — надо смотреть на сам отвергнутый
запрос.

Рекордер встаёт МЕЖДУ `dsh` и настоящим провайдером: `dsh` видит его как
эндпоинт, а он передаёт запрос дальше и пишет обе стороны дословно. Заглушкой
это сделать нельзя — заглушка отвечает наше представление о провайдере, а
нужен ответ провайдера (AGENTS.md: «Заглушка внешнего инструмента — это
пересказ, и она ломается на исправном коде»).

Что пишется в перехват:
 - строка запроса, заголовки (`x-api-key` не печатается вовсе) и тело целиком;
 - код ответа провайдера, его заголовки и тело дословно;
 - вердикт «ответ модели / отказ» — по телу, не по коду: у этих провайдеров
   HTTP 200 стоит и под ответом, и под ошибкой (#1520).

Запуск: шаг «Перехват запроса dsh (#1525)» в `.github/workflows/quotas.yml` —
секреты цепочки есть только там.
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# Правило URL и разбор ответа — из зонда #1520, не вторая копия рядом
# (AGENTS.md, «Одно место правды»). Разойдись они, перехват ходил бы не тем
# маршрутом, которым ходит замер, и сравнивать их стало бы нельзя.
_probe_spec = importlib.util.spec_from_file_location(
    "anthropic_route_probe", Path(__file__).resolve().parent / "anthropic_route_probe.py")
probe = importlib.util.module_from_spec(_probe_spec)
_probe_spec.loader.exec_module(probe)

#: Заголовки, которые не пересылаются провайдеру: их проставит сам
#: `urllib`/сервер, а чужие значения ломают пересылку (`host` увёл бы запрос,
#: `content-length` разошёлся бы с телом, `accept-encoding` заставил бы
#: провайдера сжать ответ, который мы обязаны показать читаемым).
HOP_BY_HOP = frozenset({
    "host", "content-length", "connection", "accept-encoding",
    "transfer-encoding", "keep-alive",
})

#: Заголовки, которые не печатаются НИКОГДА, даже замаскированными: значение
#: секрета не должно попасть в лог публичного репозитория ни в каком виде
#: (AGENTS.md, «Секреты»; маскирование ловит точное совпадение, а заголовок
#: может нести производное — например схему `Bearer <ключ>`).
NEVER_PRINTED_HEADERS = frozenset({"x-api-key", "authorization", "proxy-authorization"})


def forward_target(local_path: str, real_base_url: str) -> str:
    """Куда переслать запрос, пришедший на локальный путь.

    `dsh` строит адрес правилом плагина от НАШЕГО base_url, поэтому путь
    приходит вида `/v1/messages`. Пересылать надо по тому же правилу от
    настоящего base_url — иначе перехват мерил бы другой маршрут, и отказ
    провайдера относился бы не к тому запросу."""
    root = probe.messages_api_root(real_base_url)
    suffix = local_path[3:] if local_path.startswith("/v1") else local_path
    return f"{root}{suffix}"


class Recorder(BaseHTTPRequestHandler):
    """Пересылает запрос настоящему провайдеру и пишет обе стороны.

    Атрибуты `target_base`, `secret`, `captures` ставятся на класс перед
    запуском сервера: `HTTPServer` создаёт обработчик на каждый запрос, и
    держать состояние в экземпляре негде."""

    target_base = ""
    secret = ""
    captures: list[dict] = []
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 — имя задано BaseHTTPRequestHandler
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        url = forward_target(self.path, self.target_base)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP}
        headers["x-api-key"] = self.secret
        record = {
            "path": self.path,
            "forwarded_to": url,
            "request_headers": {k: v for k, v in headers.items()
                                if k.lower() not in NEVER_PRINTED_HEADERS},
            "request_body": body.decode("utf-8", "replace"),
        }
        try:
            request = urllib.request.Request(url, data=body, method="POST",
                                             headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                record.update(status=response.status,
                              response_headers=dict(response.headers))
        except urllib.error.HTTPError as error:
            payload = error.read()
            record.update(status=error.code, response_headers=dict(error.headers))
        except OSError as error:
            # «Возможности нет» отделено от «возможность есть, но отказала»:
            # сеть до провайдера — не его ответ (AGENTS.md, fail loud).
            payload = b""
            record.update(status=0, response_headers={},
                          transport_error=f"{error}")
        text = payload.decode("utf-8", "replace")
        record["response_body"] = text
        record["is_answer"] = probe.classify_answer(record["status"], text)
        record["error_type"] = probe.answer_error_type({"body": text})
        record["attempt"] = len(self.captures) + 1
        self.captures.append(record)

        self.send_response(record["status"] or 502)
        for key, value in record["response_headers"].items():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        """Молчим: собственный лог сервера перемешал бы перехват с шумом, а
        всё нужное уже лежит в `captures`."""


def run_dsh(prompt: str, port: int, model: str, timeout: int) -> tuple[int, str]:
    """Настоящий `dsh` на настоящем профиле — зовём то же, что зовёт CI.

    Профиль собирается тем же `dsh_patch_profile` из `scripts/lib/dsh-ci.sh`,
    что и в прогоне: свой профиль здесь означал бы, что перехвачен запрос,
    которого конвейер не делает."""
    env = dict(os.environ)
    env.update(
        DEEPSEEK_BASE_URL=f"http://127.0.0.1:{port}/v1",
        DEEPSEEK_MODEL=model,
        # ASCII и ничего больше: dsh проверяет ключ ДО запроса и отвергает
        # всё, что не влезает в HTTP-заголовок (живой отказ, прогон
        # 36004997980: «the API key resolved from DEEPSEEK_API_KEY contains
        # characters no HTTP header can carry»). Кириллица тут выглядела
        # понятнее, но перехват из-за неё не сделал НИ ОДНОГО запроса.
        # Значение подставное: локальный адрес секрета не требует, а
        # настоящий ключ рекордер подставляет сам при пересылке.
        DEEPSEEK_API_KEY="local-recorder-placeholder-not-a-secret",
        NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
    )
    patch = subprocess.run(
        ["bash", "-c",
         'source scripts/lib/dsh-ci.sh >/dev/null 2>&1; dsh_patch_profile headless'],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if patch.returncode != 0:
        return patch.returncode, f"профиль не собран: {patch.stderr.strip()}"
    run = subprocess.run(["dsh", "--profile", "headless", prompt], env=env,
                         capture_output=True, text=True, encoding="utf-8",
                         timeout=timeout)
    return run.returncode, (run.stderr or run.stdout).strip()


def format_capture(record: dict, secrets: list[str]) -> str:
    lines = [f"── попытка {record.get('attempt', 1)}: запрос на {record['path']} "
             f"→ переслан на {record['forwarded_to']}"]
    for key, value in sorted(record["request_headers"].items()):
        lines.append(f"    {key}: {value}")
    lines.append("")
    lines.append("тело запроса dsh (дословно):")
    lines.append(probe.redact(record["request_body"], secrets))
    lines.append("")
    verdict = ("ОТВЕТ МОДЕЛИ" if record["is_answer"]
               else f"ОТКАЗ ({record['error_type'] or 'тип не назван'})")
    lines.append(f"── ответ провайдера: HTTP {record['status']} — {verdict}")
    if record.get("transport_error"):
        lines.append(f"    до провайдера не дошли: {record['transport_error']}")
    lines.append(probe.redact(record["response_body"], secrets) or "(пустое тело)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", default="OpenRouter-1",
                        help="имя записи цепочки, к которой пересылать")
    parser.add_argument("--prompt", default="ping",
                        help="промпт для dsh; короткий по умолчанию — ловим "
                             "отказ, а не меряем длину")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    chain = probe.load_chain()
    entry = next((e for e in chain if e["name"] == args.entry), None)
    if entry is None:
        names = ", ".join(e["name"] for e in chain) or "цепочка пуста"
        print(f"::error::записи «{args.entry}» нет в config/provider-usage.json "
              f"(есть: {names}) — перехват невозможен; это «возможности нет», "
              "а не «провайдер отверг»", file=sys.stderr)
        return 1
    secret = os.environ.get(entry["secret_env"], "")
    if not secret:
        print(f"::error::секрета {entry['secret_env']} нет в окружении — "
              "перехват поймал бы отказ по ключу, а не тот, который "
              "расследуется (#1525)", file=sys.stderr)
        return 1

    Recorder.target_base = entry["base_url"]
    Recorder.secret = secret
    Recorder.captures = []
    server = HTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    model = probe.entry_models(entry)[0]
    try:
        rc, tail = run_dsh(args.prompt, server.server_port, model, args.timeout)
    except subprocess.TimeoutExpired:
        rc, tail = 124, f"dsh не завершился за {args.timeout}с"
    finally:
        server.shutdown()

    print(f"запись: {entry['name']} ({entry['base_url']}, модель {model})")
    print(f"dsh завершился с кодом {rc}; хвост stderr: {probe.redact(tail, [secret])}")
    print(f"перехвачено запросов: {len(Recorder.captures)}")
    print("")
    for record in Recorder.captures:
        print(format_capture(record, [secret]))
        print("")

    rejected = [r for r in Recorder.captures if not r["is_answer"]]
    if not Recorder.captures:
        print("::warning::dsh не сделал ни одного запроса — перехватывать "
              "нечего; отказ случился ДО обращения к провайдеру, и искать его "
              "надо в нашем коде, а не в чужом ответе")
        return 0
    if not rejected:
        print("::warning::все перехваченные запросы провайдер принял — "
              "воспроизвести отказ этим промптом не удалось; это факт замера, "
              "а не доказательство исправности: расследуемый отказ случается "
              "на промпте ai-review, который длиннее")
    return 0


if __name__ == "__main__":
    sys.exit(main())
