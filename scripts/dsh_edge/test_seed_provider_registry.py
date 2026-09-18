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
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SCRIPT = Path(__file__).resolve().parent / "seed_provider_registry.py"
SECRET_VALUE = "sk-живой-ключ-который-не-должен-утечь-12345"

_spec = importlib.util.spec_from_file_location("seed_provider_registry_under_test", SCRIPT)
seed_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_module)


class FakeMorda:
    """Морда в прод-форме: копит записанные профили, отдаёт их в describe.

    Прод-форма здесь означает И фильтр Cloudflare перед приложением (#225):
    запрос с библиотечным User-Agent режется до веток ниже. Зелёный тест —
    значит клиент засева несёт явное собственное имя, а не то, что сервер
    ест всё подряд."""

    def __init__(self, *, namespace_mounted: bool = True, preset: dict | None = None,
                 login_status: int = 303):
        self.namespace_mounted = namespace_mounted
        self.providers: dict = dict(preset or {})
        self.calls: list[tuple[str, dict]] = []
        self.credentials: dict[str, str] = {}
        self.redirect_target_hits = 0
        self.login_status = login_status
        self.login_user_agents: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # тишина в выводе теста
                pass

            def _send(self, result: dict, code: int = 200):
                # Прод-форма ответа RPC — конверт server-response с эхом rpcId
                # (docs/research/12-dsh-edge-session-api.md, dsh-edge/registry-
                # integration/check.mjs читает .result из того же конверта).
                body = json.dumps({"type": "server-response",
                                   "rpcId": getattr(self, "_rpc_id", ""),
                                   "result": result}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 — цель 303-редиректа логина
                # ПРОД-ФОРМА (#1337, живой прогон 35187895910): цель редиректа
                # логина отвечает 403, а не 200. Раньше фикстура отдавала 200 —
                # наш ПЕРЕСКАЗ поведения морды, и потому не поймала, что клиент
                # по редиректу вообще ходит. Правило репозитория: тест кормит
                # прод-форму, а не пересказ. Зелёный тест тут означает, что
                # клиент по редиректу НЕ пошёл.
                outer.redirect_target_hits += 1
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):  # noqa: N802 — имя задано BaseHTTPRequestHandler
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode() if length else ""
                method = self.path.rsplit("/", 1)[-1]
                # Гвардия класса #225 — фильтр Cloudflare ПЕРЕД приложением:
                # библиотечный User-Agent (дефолт urllib.request) режется
                # 403'м text/plain «error code: 1010», до веток ниже запрос
                # не доходит и в calls не попадает. Форма скопирована с живой
                # морды (docs/research/12-dsh-edge-session-api.md).
                if self.headers.get("User-Agent", "").startswith("Python-urllib"):
                    body = b"error code: 1010"
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if method == "login":
                    outer.login_user_agents.append(self.headers.get("User-Agent", ""))
                    outer.calls.append(("auth.login", {}))
                    if outer.login_status != 303:
                        # Прод-форма ответа приложения на неверный ключ:
                        # 401 + конверт ошибки приложения (research/12:
                        # «curl/8.x → 401 (ответ приложения)», ошибки
                        # приложения — {ok:false,error}).
                        payload = json.dumps({"ok": False, "error": {
                            "code": "unauthorized", "message": "bad accessKey"}}).encode()
                        self.send_response(outer.login_status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    self.send_response(303)
                    self.send_header("Set-Cookie", "__Host-dsh_edge_owner=fake; Path=/")
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                # ПРОД-ФОРМА RPC (живой прогон 35330225676): тело — конверт
                # client-request {type, rpcId, method, payload}; хост строит
                # RpcRequest из конверта и голый payload НЕ разворачивает
                # (settings.mutate отказал с «for "undefined"», payload не
                # дошёл). Фикстура требует конверт так же — раньше она читала
                # голый payload, то есть кормилась нашим пересказом, и девиация
                # клиента была бы зелёной, как в живом прогоне.
                try:
                    envelope = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    envelope = {}
                if (envelope.get("type") != "client-request"
                        or envelope.get("method") != method):
                    self._rpc_id = str(envelope.get("rpcId") or "")
                    return self._send({"ok": False, "error": {
                        "code": "bad-request",
                        "message": (f"ожидал конверт client-request c method={method!r}, "
                                    f"получено type={envelope.get('type')!r}")}})
                self._rpc_id = str(envelope.get("rpcId") or "")
                payload = envelope.get("payload") or {}
                outer.calls.append((method, payload))
                if method == "settings.describe":
                    namespaces = [{"ns": "llm-pi-ai"}] if outer.namespace_mounted else []
                    return self._send({"ok": True, "value": {
                        "namespaces": namespaces, "writable": True,
                        "settings": {"llm-pi-ai": {"providers": outer.providers}}}})
                if method == "settings.mutate":
                    for op in payload.get("ops", []):
                        outer.providers[op["path"][1]] = op["value"]
                    return self._send({"ok": True, "value": {}})
                if method == "credentials.set":
                    outer.credentials[payload["ref"]] = payload["value"]
                    return self._send({"ok": True, "value": {}})
                if method == "llm.providers":
                    rows = [{"provider": route, "active": outer.providers[route]["apiKeyEnv"]
                             in outer.credentials, "settingsNs": "llm-pi-ai"}
                            for route in outer.providers]
                    return self._send({"ok": True, "value": {"providers": rows}})
                return self._send({"ok": False, "error": {"message": f"нет метода {method}"}})

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


def test_login_does_not_follow_the_303_redirect(morda):
    """#1337: успех логина — это САМ 303; поход на цель редиректа отвечает 403
    и маскировал успешный логин отказом, вина ложно падала на ключ."""
    result = run_seed(morda.origin, extra_env={"DEEPSEEK_API_KEY": SECRET_VALUE})
    assert result.returncode == 0, result.stderr
    assert morda.redirect_target_hits == 0, (
        "клиент пошёл по 303-редиректу — цель отвечает 403, и логин будет "
        "объявлен отказом, хотя он удался")


def test_seed_carries_explicit_user_agent_past_cf_filter(morda):
    """#225 (находитка ревью PR #1338): фильтр CF в фикстуре вооружён, значит
    зелёный прогон означает, что клиент засева дошёл до приложения с явным
    собственным именем, а не с дефолтным библиотечным Python-urllib."""
    result = run_seed(morda.origin, extra_env={"DEEPSEEK_API_KEY": SECRET_VALUE})
    assert result.returncode == 0, result.stderr
    assert morda.login_user_agents, "логин не дошёл до морды"
    assert not any(ua.startswith("Python-urllib") for ua in morda.login_user_agents), (
        f"клиент засева шлёт дефолтный библиотечный UA: {morda.login_user_agents}")


def test_cf_filter_in_fixture_bites_bare_python_urllib(morda):
    """Контроль живости фильтра наоборот: голый urllib БЕЗ явного UA режется
    403 «error code: 1010» ДО приложения — как живая Cloudflare перед мордой.
    Доказывает, что предыдущий тест зелёный не потому, что сервер ест всё."""
    data = urllib.parse.urlencode({"accessKey": "fake-access-key"}).encode()
    req = urllib.request.Request(f"{morda.origin}/api/auth/login", data=data)
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=5)
    assert excinfo.value.code == 403
    assert "error code: 1010" in excinfo.value.read().decode()
    assert ("auth.login", {}) not in morda.calls, "фильтр пропустил запрос до приложения"


def test_login_failure_message_names_the_real_cause():
    """#1337, «алерт не гадает» (находитка ревью PR #1338): данные ответа
    различают причину — фильтр CF, отказ ключа, неверный URL. Иначе владелец
    снова ротирует рабочий ключ: так 403 от CF в прогоне 35187895910 был
    прочитан как «проверь DSH_EDGE_ACCESS_KEY»."""
    describe = seed_module.describe_login_failure
    cf = describe(403, "text/plain", "error code: 1010")
    assert "Cloudflare" in cf and "1010" in cf
    assert "DSH_EDGE_ACCESS_KEY" not in cf, (
        "фильтр CF стоит ДО приложения — ключ не проверялся, текст не вправе винить его")
    key = describe(401, "application/json", '{"ok":false,"error":{}}')
    assert "DSH_EDGE_ACCESS_KEY" in key, "401 — отказ ключа приложением, текст обязан это назвать"
    url = describe(404, "application/json", "not found")
    assert "DSH_EDGE_URL" in url
    unknown = describe(500, "", "")
    assert "нельзя" in unknown, "данных для причины нет — так и сказать, не перечислять гипотезы"


def test_real_login_rejection_is_loud_and_writes_nothing():
    """Критерий 2 задачи: на НАСТОЯЩЕМ отказе логина (морда ответила 401)
    прогон красный, причина названа, в морду не записано ничего."""
    server = FakeMorda(login_status=401)
    try:
        result = run_seed(server.origin)
        assert result.returncode == 1, result.stderr
        assert "не дал 303" in result.stderr
        assert "DSH_EDGE_ACCESS_KEY" in result.stderr, (
            "401 — это ответ приложения, отказ ключа: текст обязан назвать его")
        assert server.mutated_routes() == [], "писал в морду мимо отказанного логина"
        assert "fake-access-key" not in result.stderr, "значение ключа утекло в отказ"
    finally:
        server.stop()


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
