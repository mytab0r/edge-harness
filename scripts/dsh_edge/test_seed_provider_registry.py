#!/usr/bin/env python3
"""Гвардия засева реестра морды (#1330) — ПОВЕДЕНЧЕСКАЯ, не структурная.

Правило репозитория, оплаченное #891/#893: гвардия, проверяющая НАЛИЧИЕ имён в
исходнике, красится ложно-зелёной при переименовании и молча пропускает
вырезанное тело. Поэтому здесь поднимается настоящий HTTP-сервер, говорящий
RPC-конвертом морды, а скрипт запускается отдельным процессом — ровно так, как
он пойдёт в проде. Проверяется наблюдаемое поведение: какие ops реально ушли на
провод, повторный прогон, и что значение ключа не вытекло ни в один поток.

Форма ответов сервера скопирована с прод-контракта, а не пересказана: см.
dsh-edge/registry-integration/check.mjs (settings.describe → namespaces[],
llm.providers → {providers:[{provider,active,settingsNs}]}, конверт {result:{ok,value}}).
"""
# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SCRIPT = Path(__file__).resolve().parent / "seed_provider_registry.py"
SECRET_VALUE = "sk-живой-ключ-который-не-должен-утечь-12345"


class FakeMorda:
    """Морда в прод-форме: копит записанные профили, отдаёт их в describe."""

    def __init__(self, *, namespace_mounted: bool = True, preset: dict | None = None):
        self.namespace_mounted = namespace_mounted
        self.providers: dict = dict(preset or {})
        self.calls: list[tuple[str, dict]] = []
        self.credentials: dict[str, str] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # тишина в выводе теста
                pass

            def _send(self, payload: dict, code: int = 200):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 — цель 303-редиректа логина
                # urllib идёт по редиректу GET'ом; без этого обработчика
                # BaseHTTPRequestHandler отвечает 501 и логин "падает".
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):  # noqa: N802 — имя задано BaseHTTPRequestHandler
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                method = self.path.rsplit("/", 1)[-1]
                if method == "login":
                    outer.calls.append(("auth.login", {}))
                    self.send_response(303)
                    self.send_header("Set-Cookie", "__Host-dsh_edge_owner=fake; Path=/")
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.loads(raw or "{}")
                outer.calls.append((method, payload))
                if method == "settings.describe":
                    namespaces = [{"ns": "llm-pi-ai"}] if outer.namespace_mounted else []
                    return self._send({"result": {"ok": True, "value": {
                        "namespaces": namespaces, "writable": True,
                        "settings": {"llm-pi-ai": {"providers": outer.providers}}}}})
                if method == "settings.mutate":
                    for op in payload.get("ops", []):
                        outer.providers[op["path"][1]] = op["value"]
                    return self._send({"result": {"ok": True, "value": {}}})
                if method == "credentials.set":
                    outer.credentials[payload["ref"]] = payload["value"]
                    return self._send({"result": {"ok": True, "value": {}}})
                if method == "llm.providers":
                    rows = [{"provider": route, "active": outer.providers[route]["apiKeyEnv"]
                             in outer.credentials, "settingsNs": "llm-pi-ai"}
                            for route in outer.providers]
                    return self._send({"result": {"ok": True, "value": {"providers": rows}}})
                return self._send({"result": {"ok": False, "error": {"message": f"нет метода {method}"}}})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()

    def mutated_routes(self) -> list[str]:
        return [op["path"][1] for method, payload in self.calls if method == "settings.mutate"
                for op in payload.get("ops", [])]


def run_seed(origin: str | None, *, extra_env: dict | None = None, args: list[str] | None = None):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp",
           "DSH_EDGE_ACCESS_KEY": "fake-access-key"}
    if origin:
        env["DSH_EDGE_URL"] = origin
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(SCRIPT), *(args or [])],
                          capture_output=True, text=True, encoding="utf-8", env=env)


@pytest.fixture
def morda():
    server = FakeMorda()
    yield server
    server.stop()


def test_seeds_every_manifest_route_once(morda):
    """Пустой реестр → все маршруты манифеста записаны ровно по разу."""
    result = run_seed(morda.origin, extra_env={"DEEPSEEK_API_KEY": SECRET_VALUE})
    assert result.returncode == 0, result.stderr
    routes = morda.mutated_routes()
    assert sorted(routes) == sorted(set(routes)), f"маршрут записан дважды: {routes}"
    assert "glm" in routes and "nvidia-nim-1" in routes
    assert len(routes) == 8, f"ожидались все 8 маршрутов манифеста, ушло {routes}"


def test_second_run_is_noop(morda):
    """Идемпотентность — повтор не делает НИ ОДНОЙ записи."""
    first = run_seed(morda.origin, extra_env={"DEEPSEEK_API_KEY": SECRET_VALUE})
    assert first.returncode == 0, first.stderr
    morda.calls.clear()
    second = run_seed(morda.origin, extra_env={"DEEPSEEK_API_KEY": SECRET_VALUE})
    assert second.returncode == 0, second.stderr
    assert morda.mutated_routes() == [], "повторный прогон переписал профили — не идемпотентен"
    assert "уже совпадает" in second.stdout


def test_key_value_never_reaches_output(morda):
    """Значение ключа не печатается НИ В ОДИН поток — репозиторий публичный."""
    result = run_seed(morda.origin, extra_env={"DEEPSEEK_API_KEY": SECRET_VALUE})
    assert result.returncode == 0, result.stderr
    assert SECRET_VALUE not in result.stdout, "значение ключа утекло в stdout"
    assert SECRET_VALUE not in result.stderr, "значение ключа утекло в stderr"
    # Контроль живости проверки: ключ ДОЛЖЕН был реально уйти на провод —
    # иначе тест выше зелёный просто потому, что ключа нигде нет.
    assert morda.credentials.get("DEEPSEEK_API_KEY") == SECRET_VALUE


def test_missing_namespace_fails_loud():
    """Плагин реестра не смонтирован → громкий отказ, не молчаливый успех."""
    server = FakeMorda(namespace_mounted=False)
    try:
        result = run_seed(server.origin)
        assert result.returncode == 1
        assert "не смонтирован" in result.stderr
        assert server.mutated_routes() == [], "писал в морду, где namespace нет"
    finally:
        server.stop()


def test_missing_config_fails_loud_and_separately():
    """Нет DSH_EDGE_URL → отдельный код и отдельное сообщение, не общий провал."""
    result = run_seed(None)
    assert result.returncode == 2, result.stderr
    assert "DSH_EDGE_URL" in result.stderr
    assert "--dry-run" in result.stderr, "отказ обязан назвать, чем проверить без морды"


def test_dry_run_touches_nothing(morda):
    """--dry-run не ходит в морду вовсе (и не требует ключа)."""
    result = run_seed(morda.origin, args=["--dry-run"])
    assert result.returncode == 0, result.stderr
    assert morda.calls == [], "dry-run сходил в морду"
    assert "glm" in result.stdout


def test_priority_loss_is_stated_aloud(morda):
    """#1329 назван в выводе: перенесён набор, НЕ приоритет."""
    result = run_seed(morda.origin, args=["--dry-run"])
    assert "Приоритет между провайдерами НЕ переносится" in result.stdout
    assert "#1329" in result.stdout


def test_reserved_route_is_skipped_not_attempted(morda, tmp_path):
    """Зарезервированный мордой маршрут не трогаем вовсе, а не ловим отказ."""
    manifest = {"chains": {"c": [
        {"name": "deepseek-official", "base_url": "https://x.example/v1",
         "model": "m", "secret_env": "K", "max_output_tokens": 128},
        {"name": "GLM", "base_url": "https://api.z.ai/v4",
         "model": "glm-5.3-flash", "secret_env": "DEEPSEEK_API_KEY", "max_output_tokens": 128}]}}
    path = tmp_path / "m.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = run_seed(morda.origin, args=["--manifest", str(path)])
    assert result.returncode == 0, result.stderr
    assert morda.mutated_routes() == ["glm"], "тронул зарезервированный маршрут"
    assert "зарезервирован" in result.stdout
