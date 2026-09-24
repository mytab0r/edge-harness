#!/usr/bin/env python3
"""Тесты зонда Anthropic-маршрута (#1520).

Сеть не трогаем: сам сетевой шов тривиален, а проверять надо две вещи, на
которых уже ошибались — ПРАВИЛО URL (неверный пересказ дал в #1502 таблицу,
неверную для трёх строк из четырёх) и то, что секреты не утекают в вывод.

Запуск: python -m pytest scripts/measure/test_anthropic_route_probe.py -q
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

import pytest

_spec = importlib.util.spec_from_file_location(
    "anthropic_route_probe",
    Path(__file__).resolve().parent / "anthropic_route_probe.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


#: Формы base_url из боевой цепочки и ожидаемый корень по правилу плагина
#: (`@deepseek-ai/dsh-llm-deepseek/lib/index.js::messagesApiRoot`). Каждая
#: строка — живая запись, а не выдуманный пример: именно на этих четырёх
#: формах пересказ правила разошёлся с исходником (#1502).
ROOT_CASES = [
    ("https://api.z.ai/api/coding/paas/v4", "https://api.z.ai/api/coding/paas/v4/v1"),
    ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
    ("https://integrate.api.nvidia.com/v1", "https://integrate.api.nvidia.com/v1"),
    ("https://ollama.com/v1/", "https://ollama.com/v1"),
]


@pytest.mark.parametrize("base,expected", ROOT_CASES, ids=[c[0] for c in ROOT_CASES])
def test_messages_api_root_matches_the_plugin_rule(base: str, expected: str):
    """`/v1` дописывается ТОЛЬКО если путь им ещё не заканчивается.

    Ошибка здесь не абстрактная: пересказ этого правила своими словами дал в
    #1502 таблицу «удвоение пути у всех четырёх», верную ровно для одной
    записи — и вывод «все восемь бьют в несуществующий маршрут» был неверен."""
    assert mod.messages_api_root(base) == expected


def test_trailing_slashes_are_stripped_before_the_check():
    """`replace(/\\/+$/u, "")` в исходнике снимает ВСЕ хвостовые слэши, а не
    один: без этого `…/v1//` не распознался бы как уже заканчивающийся на
    `/v1` и получил бы второй `/v1`."""
    assert mod.messages_api_root("https://ollama.com/v1///") == "https://ollama.com/v1"


def test_secret_value_never_reaches_the_report():
    """Секрет уходит в заголовок запроса и не должен возвращаться в вывод.

    Провайдер вполне может отразить присланный ключ в тексте ошибки — тогда
    он попал бы в лог публичного репозитория. Маскирование точных значений
    здесь обязательно, и его предел назван честно в докстринге `redact`."""
    secret = "sk-not-a-real-key-000111222333"
    body = f'{{"error":{{"message":"invalid api key: {secret}"}}}}'

    assert secret not in mod.redact(body, [secret])
    assert "***" in mod.redact(body, [secret])


def test_short_values_are_not_masked_to_avoid_shredding_the_body():
    """Короткие значения не маскируются намеренно: секрет короче восьми
    символов маскировать нечем — подстрока такой длины встречается в обычном
    тексте, и замена изрешетила бы тело ответа до нечитаемости, то есть убила
    бы ровно то, ради чего зонд написан."""
    assert mod.redact("boom: abc", ["abc"]) == "boom: abc"


def test_report_carries_code_and_body_but_not_the_secret_value():
    """Отчёт обязан нести и код, и тело: код один отвечает «не приняли», а
    тело — «чем именно не устроило», и второе тут главное."""
    results = [{
        "name": "OpenRouter-1", "status": 400, "model": "nvidia/nemotron",
        "max_tokens": 131072,
        "url": "https://openrouter.ai/api/v1/messages",
        "secret_env": "OPENROUTER_API_KEY", "secret_present": True,
        "body": '{"error":{"message":"Invalid Anthropic Messages API request"}}',
    }]
    report = mod.format_report(results)

    assert "OpenRouter-1" in report
    assert "400" in report
    assert "Invalid Anthropic Messages API request" in report, (
        "тело ответа обязано попадать в отчёт — оно и есть ответ на вопрос задачи")
    assert "OPENROUTER_API_KEY" in report, "имя переменной — можно, значение — нет"


def test_chain_is_read_from_the_manifest_not_hardcoded(tmp_path):
    """Цепочка берётся из `config/provider-usage.json` — то же единственное
    место правды, что у `provider_latency.py` и `dsh-ci.sh`. Вторая таблица
    кандидатов рядом была бы отложенным расхождением."""
    manifest = tmp_path / "usage.json"
    manifest.write_text(json.dumps({
        "usage": {"ai-review": "main"},
        "chains": {"main": [
            {"name": "A", "base_url": "https://a/v1", "model": "m", "secret_env": "A_KEY"},
            {"name": "нет полей"},
        ]},
    }), encoding="utf-8")

    chain = mod.load_chain("ai-review", manifest)

    assert [e["name"] for e in chain] == ["A"], (
        "запись без обязательных полей обязана отбрасываться, а не ехать "
        "дальше полупустой")


def test_models_plural_entry_is_probed_not_silently_dropped(tmp_path):
    """Запись с `models` (фолбэк по моделям внутри провайдера) обязана попасть
    в замер.

    Живой случай: первый прогон зонда (run 35996106905) напечатал шесть строк
    из восьми — NVIDIA-NIM-1/2 задают модели ключом `models`, и фильтр по
    `model` выбросил их молча. Отчёт при этом выглядел полным: читатель не
    отличил бы «не спрашивали» от «ответили»."""
    manifest = tmp_path / "usage.json"
    manifest.write_text(json.dumps({
        "usage": {"ai-review": "main"},
        "chains": {"main": [
            {"name": "NIM", "base_url": "https://nim/v1", "secret_env": "K",
             "models": ["a", "b"]},
        ]},
    }), encoding="utf-8")

    chain = mod.load_chain("ai-review", manifest)

    assert [e["name"] for e in chain] == ["NIM"]
    assert mod.entry_models(chain[0]) == ["a", "b"], (
        "обе модели записи обязаны быть опрошены — прод перебирает их так же")


def test_entry_without_any_model_is_dropped_loudly(tmp_path, capsys):
    """Выброс записи обязан быть слышен (AGENTS.md, fail loud): молча
    пропущенная запись читается в отчёте как «эту не спрашивали» только тем,
    кто помнит состав цепочки наизусть."""
    manifest = tmp_path / "usage.json"
    manifest.write_text(json.dumps({
        "usage": {"ai-review": "main"},
        "chains": {"main": [
            {"name": "пустая", "base_url": "https://x/v1", "secret_env": "K",
             "models": []},
        ]},
    }), encoding="utf-8")

    assert mod.load_chain("ai-review", manifest) == []
    assert "пустая" in capsys.readouterr().err


def test_report_distinguishes_route_from_payload_by_max_tokens():
    """Главный вопрос задачи: «маршрут не тот» или «тело запроса не то».

    Различает их ровно одно — тот же URL и тот же ключ, разный `max_tokens`.
    Поэтому запись обязана нести его в отчёте: строка «200» без потолка не
    говорит, что именно проверено."""
    results = [
        {"name": "OpenRouter-1", "status": 200, "model": "m", "max_tokens": 16,
         "url": "https://openrouter.ai/api/v1/messages", "secret_env": "K",
         "secret_present": True, "body": "{}"},
        {"name": "OpenRouter-1", "status": 400, "model": "m", "max_tokens": 131072,
         "url": "https://openrouter.ai/api/v1/messages", "secret_env": "K",
         "secret_present": True, "body": "Invalid Anthropic Messages API request"},
    ]
    report = mod.format_report(results)

    assert "131072" in report and "16" in report
    assert report.count("https://openrouter.ai/api/v1/messages") >= 2, (
        "обе попытки — по одному и тому же URL; если бы отчёт печатал его "
        "один раз, различие потолка перестало бы читаться как причина")


def test_missing_manifest_gives_empty_chain_not_a_default(tmp_path):
    """Нет манифеста — пустая цепочка, а не «дефолтный провайдер»: второй
    фоллбэк-список означал бы, что зонд меряет не то, что реально зовёт
    конвейер."""
    assert mod.load_chain("ai-review", tmp_path / "нет-такого.json") == []


def test_probe_really_sends_the_extra_field_and_a_server_can_reject_it(tmp_path):
    """Настоящий сервер, настоящий запрос: заглушка тут бесполезна.

    Проверяется то, ради чего поле вообще добавлено — что `probe` кладёт его в
    ТЕЛО, а не в заголовок и не в URL. Сервер отвечает 400 ровно тем текстом,
    которым OpenRouter отвечает dsh, и принимает чистый протокол — то есть
    воспроизводит наблюдаемое различие целиком (AGENTS.md: стенд поднимает
    настоящий сервер, «недоступность» — закрытым портом, а не `return 7`)."""
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append(body)
            reject = "dsh_session_log" in body
            payload = (b'{"error":{"message":"Invalid Anthropic Messages API request"}}'
                       if reject else b'{"type":"message"}')
            self.send_response(400 if reject else 200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    entry = {"name": "стенд", "secret_env": "НЕТ_ТАКОЙ",
             "base_url": f"http://127.0.0.1:{server.server_port}/v1"}
    try:
        clean = mod.probe(entry, "m", max_tokens=16)
        dirty = mod.probe(entry, "m", max_tokens=16,
                          extra={"dsh_session_log": mod.DSH_EXTRA_FIELDS["dsh_session_log"]},
                          label="+dsh_session_log")
    finally:
        server.shutdown()

    assert clean["status"] == 200, "чистый протокол обязан проходить"
    assert dirty["status"] == 400
    assert "Invalid Anthropic Messages API request" in dirty["body"]
    assert "dsh_session_log" in seen[1] and "dsh_session_log" not in seen[0], (
        "поле обязано уходить именно в теле — иначе замер меряет не то")


def test_extra_fields_are_the_ones_dsh_actually_sends():
    """Список полей — снимок живого запроса dsh 0.1.7-alpha.2, не догадка.

    Если он разойдётся с тем, что шлёт плагин, зонд будет проверять поля,
    которых нет, и молчать о тех, что есть. Поэтому состав зафиксирован здесь
    и меняется тем же коммитом, что поднимает пин плагина."""
    assert set(mod.DSH_EXTRA_FIELDS) == {
        "thinking", "dsh_plugin_packages", "dsh_session_log", "stream", "system"}
