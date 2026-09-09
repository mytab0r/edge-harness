"""Гвардия scripts/lib/provider_secrets_import.py (задача #733, #777) —
СИНТЕТИЧЕСКИЕ фикстуры, никаких настоящих кредов. Проверяет: разбор контракта
имён (PR #732) из dsh-ci.sh, разбор формата экспорта, отказ при файле внутри
рабочего дерева, отсутствие значений в выводе (маркер фикстуры не встречается
нигде — ни в stdout/stderr, ни в теле ошибки живой пробы), выбор слотов по
ДВУМ РАЗНЫМ осям ранга (квота/backoff — не дисквалификация; ключ неверен —
дисквалификация), живая проба доступна и в сухом прогоне, идемпотентность,
argv без значений секрета.

Запуск: python -m pytest scripts/lib/test_provider_secrets_import.py -q
"""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provider_secrets_import as psi  # noqa: E402


def _read_request(conn: socket.socket) -> bytes:
    """Читает запрос до конца заголовков — минимально нужное, чтобы h.request()
    в urllib успел уйти на сервер до того, как сервер сломает соединение."""
    conn.settimeout(5)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


class _RawTcpServer:
    """Сырой TCP-сервер (не мок) для веток probe_provider, которые urllib НЕ
    оборачивает в URLError — обрыв/таймаут/мусор вместо статус-строки
    (блокирующая 1 гейта PR #778): h.getresponse() внутри urlopen() их не
    ловит, только h.request()."""

    def __init__(self, handler):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, args=(handler,), daemon=True)
        self._thread.start()

    def _serve(self, handler):
        self._sock.settimeout(5)
        try:
            conn, _addr = self._sock.accept()
        except OSError:
            return
        try:
            handler(conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._sock.close()
        self._thread.join(timeout=3)

# Маркер фикстуры — заведомо не похож на реальный секрет, но узнаваем в выводе,
# если защита печати сломается (мутационная проба, см. test класса «маркер»).
FIXTURE_MARKER = "FIXTURE-MARKER-6f3ab21c9e77-DO-NOT-LEAK"

DSH_CI_FIXTURE = """#!/usr/bin/env bash
PLUGINS_SUITE_CANDIDATE_ROUTES=(
  "openrouter-1|https://openrouter.ai/api/v1|OPENROUTER_1_API_KEY|model-a|200000|OpenRouter account 1"
  "openrouter-2|https://openrouter.ai/api/v1|OPENROUTER_2_API_KEY|model-a|200000|OpenRouter account 2"
  "nvidia-nim-1|https://integrate.api.nvidia.com/v1|NVIDIA_NIM_1_API_KEY|model-b|131072|NVIDIA NIM account 1"
  "nvidia-nim-2|https://integrate.api.nvidia.com/v1|NVIDIA_NIM_2_API_KEY|model-b|131072|NVIDIA NIM account 2"
  "zai-1|https://api.z.ai/api/coding/paas/v4|ZAI_1_API_KEY|model-c|202752|Z.AI Coding Plan"
)
"""

DSH_CI_FIXTURE_NO_CONTRACT = """#!/usr/bin/env bash
echo "нет никакого контракта имён здесь"
"""


def _account(
    provider, priority, is_active, test_status, api_key,
    email="acct@example.test", error_code=None, backoff_level=0,
):
    return {
        "id": f"{provider}-{priority}",
        "provider": provider,
        "authType": "apikey",
        "email": email,
        "priority": priority,
        "isActive": is_active,
        "testStatus": test_status,
        "backoffLevel": backoff_level,
        "lastError": "",
        "errorCode": error_code,
        "apiKey": api_key,
    }


def _export_fixture():
    return {
        "providerConnections": [
            # openrouter: 3 кандидата на 2 слота — третий (худший ранг) уходит в overflow
            _account("openrouter", 1, True, "unavailable", f"{FIXTURE_MARKER}-or1"),
            _account("openrouter", 2, True, "unavailable", f"{FIXTURE_MARKER}-or2"),
            _account("openrouter", 3, True, "active", f"{FIXTURE_MARKER}-or3"),
            # nvidia: 1 кандидат на 2 слота — второй слот остаётся пустым
            _account("nvidia", 1, True, "unavailable", f"{FIXTURE_MARKER}-nv1"),
            # zai: 1 неактивная учётка — единственный кандидат, всё равно выбирается
            _account("glm", 1, False, "unavailable", f"{FIXTURE_MARKER}-glm1"),
            # вне области suite — не должен попасть ни в один слот
            _account("kimi", 1, True, "active", f"{FIXTURE_MARKER}-kimi1"),
        ],
        "settings": {"password": f"{FIXTURE_MARKER}-unrelated"},
    }


@pytest.fixture()
def dsh_ci_path(tmp_path: Path) -> Path:
    path = tmp_path / "dsh-ci.sh"
    path.write_text(DSH_CI_FIXTURE, encoding="utf-8")
    return path


@pytest.fixture()
def export_path(tmp_path: Path) -> Path:
    path = tmp_path / "export.json"
    path.write_text(json.dumps(_export_fixture()), encoding="utf-8")
    return path


# ── parse_suite_routes ───────────────────────────────────────────────────


def test_parse_suite_routes_reads_contract(dsh_ci_path):
    routes = psi.parse_suite_routes(dsh_ci_path)
    by_env = {r.secret_env: r for r in routes}
    assert set(by_env) == {
        "OPENROUTER_1_API_KEY", "OPENROUTER_2_API_KEY",
        "NVIDIA_NIM_1_API_KEY", "NVIDIA_NIM_2_API_KEY",
        "ZAI_1_API_KEY",
    }
    assert by_env["OPENROUTER_1_API_KEY"].family == "openrouter"
    assert by_env["OPENROUTER_1_API_KEY"].slot == 1
    assert by_env["ZAI_1_API_KEY"].family == "zai"


def test_parse_suite_routes_missing_contract_fails_loud(tmp_path):
    path = tmp_path / "dsh-ci.sh"
    path.write_text(DSH_CI_FIXTURE_NO_CONTRACT, encoding="utf-8")
    with pytest.raises(psi.LoudError, match="PLUGINS_SUITE_CANDIDATE_ROUTES"):
        psi.parse_suite_routes(path)


def test_parse_suite_routes_missing_file_fails_loud(tmp_path):
    with pytest.raises(psi.LoudError):
        psi.parse_suite_routes(tmp_path / "does-not-exist.sh")


# ── load_export / _ensure_outside_repo ───────────────────────────────────


def test_ensure_outside_repo_rejects_path_within_worktree():
    inside = psi.REPO_ROOT / "scripts" / "lib" / "hypothetical-export.json"
    with pytest.raises(psi.LoudError, match="рабочего дерева репозитория"):
        psi._ensure_outside_repo(inside)


def test_ensure_outside_repo_accepts_path_outside(tmp_path):
    outside = tmp_path / "export.json"
    # Не должно бросить исключение.
    resolved = psi._ensure_outside_repo(outside)
    assert resolved == outside.resolve()


def test_load_export_rejects_unrecognized_schema(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"someOtherKey": [1, 2, 3], "anotherKey": {}}), encoding="utf-8")
    with pytest.raises(psi.LoudError) as excinfo:
        psi.load_export(str(path))
    message = str(excinfo.value)
    assert "someOtherKey" in message and "anotherKey" in message
    assert "providerConnections" in message


def test_load_export_recognizes_real_schema(export_path):
    data = psi.load_export(str(export_path))
    accounts = psi.accounts_from_export(data)
    assert len(accounts) == 6


# ── select_accounts: правило isActive/testStatus/priority ────────────────


def test_select_accounts_ranking_overflow_out_of_scope(export_path, dsh_ci_path):
    data = psi.load_export(str(export_path))
    routes = psi.parse_suite_routes(dsh_ci_path)
    accounts = psi.accounts_from_export(data)
    selection = psi.select_accounts(accounts, routes)

    by_secret = {a.route.secret_env: a for a in selection.assignments}

    # openrouter: testStatus=active (or3) обгоняет priority=1/2 с unavailable.
    assert by_secret["OPENROUTER_1_API_KEY"].account.email == "acct@example.test"
    or1_key = by_secret["OPENROUTER_1_API_KEY"].account.api_key
    or2_key = by_secret["OPENROUTER_2_API_KEY"].account.api_key
    assert or1_key.endswith("-or3")  # active обгоняет unavailable
    assert or2_key.endswith("-or1")  # из оставшихся priority=1 лучше priority=2
    assert len(selection.overflow) == 1
    assert selection.overflow[0].api_key.endswith("-or2")

    # nvidia: 1 кандидат на 2 слота — второй слот пуст.
    assert by_secret["NVIDIA_NIM_1_API_KEY"].account.api_key.endswith("-nv1")
    assert by_secret["NVIDIA_NIM_2_API_KEY"].account is None

    # zai: единственная (неактивная) учётка всё равно выбрана — газ у пробы после.
    assert by_secret["ZAI_1_API_KEY"].account.api_key.endswith("-glm1")
    assert by_secret["ZAI_1_API_KEY"].account.is_active is False

    # kimi вне списка маршрутов — не должен попасть ни в assignments, ни в overflow.
    assigned_or_overflow_keys = {a.account.api_key for a in selection.assignments if a.account} | {
        a.api_key for a in selection.overflow
    }
    assert not any(k and k.endswith("-kimi1") for k in assigned_or_overflow_keys)
    assert len(selection.out_of_scope) == 1
    assert selection.out_of_scope[0].provider == "kimi"


# ── Отсутствие значений в выводе (маркер, мутационно доказуемо) ──────────


def test_no_marker_leak_in_dry_run(monkeypatch, export_path, dsh_ci_path, capsys):
    """Задача #777: живая проба теперь ЗОВЁТСЯ и в сухом прогоне (кандидатов
    на слоты suite) — но по-прежнему НИЧЕГО не пишет и не светит значение
    (маркер) ни в stdout/stderr, ни в файлах."""
    monkeypatch.setattr(psi, "gh_repo", lambda explicit: "owner/repo")
    monkeypatch.setattr(psi, "existing_secret_names", lambda repo: set())
    monkeypatch.setattr(psi, "existing_variable_names", lambda repo: set())

    def _forbidden(*args, **kwargs):
        raise AssertionError("сухой прогон не должен звать set_secret/set_variable")

    probe_calls: list[str] = []

    def _fake_probe(base_url, api_key):
        probe_calls.append(api_key)
        return psi.ProbeResult(psi.PROBE_ALIVE, "HTTP 200")

    monkeypatch.setattr(psi, "set_secret", _forbidden)
    monkeypatch.setattr(psi, "set_variable", _forbidden)
    monkeypatch.setattr(psi, "probe_provider_full", _fake_probe)

    rc = psi.main([
        "--export-file", str(export_path),
        "--dsh-ci-path", str(dsh_ci_path),
    ])
    assert rc == 0
    # Проба реально позвана в сухом прогоне (задача #777, критерий 2) — иначе
    # этот тест не отличил бы «пробу выключили» от «пробы нет вовсе».
    assert probe_calls
    captured = capsys.readouterr()
    assert FIXTURE_MARKER not in captured.out
    assert FIXTURE_MARKER not in captured.err
    for marker_containing_call in probe_calls:
        assert FIXTURE_MARKER in marker_containing_call  # проба реально получила ключ
    # Ничего не записано ни в один файл — только в tmp_path созданное самим тестом.
    for path in export_path.parent.rglob("*"):
        if path in (export_path, dsh_ci_path) or path.is_dir():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert FIXTURE_MARKER not in content


def test_no_marker_leak_in_dry_run_no_probe_flag(monkeypatch, export_path, dsh_ci_path, capsys):
    """--no-probe по-прежнему отключает пробу целиком (не только запись)."""
    monkeypatch.setattr(psi, "gh_repo", lambda explicit: "owner/repo")
    monkeypatch.setattr(psi, "existing_secret_names", lambda repo: set())
    monkeypatch.setattr(psi, "existing_variable_names", lambda repo: set())

    def _forbidden(*args, **kwargs):
        raise AssertionError("--no-probe не должен звать ни set_secret/set_variable, ни probe_provider")

    monkeypatch.setattr(psi, "set_secret", _forbidden)
    monkeypatch.setattr(psi, "set_variable", _forbidden)
    monkeypatch.setattr(psi, "probe_provider_full", _forbidden)

    rc = psi.main([
        "--export-file", str(export_path),
        "--dsh-ci-path", str(dsh_ci_path),
        "--no-probe",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert FIXTURE_MARKER not in captured.out
    assert FIXTURE_MARKER not in captured.err


def test_probe_provider_never_leaks_key_on_http_error(monkeypatch, capsys):
    """Задача #777, критерий 6: даже если провайдер вернёт ошибку, чьё тело
    содержит подстроку ключа (гипотетическое эхо), probe_provider не читает
    msg/тело — читает только error.code. Мутация: если когда-нибудь код
    начнёт возвращать str(error) целиком, этот тест покраснеет. Блокирующая 2
    гейта PR #778: гвардия теперь читает ещё и stdout/stderr (capsys) — три
    естественные мутации (print(error), print(error.msg), запись в stderr)
    красят её, хотя возвращаемое значение осталось прежним."""
    import urllib.error

    def _raise_http_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://example.test/models", 401,
            f"unauthorized: your key {FIXTURE_MARKER} is invalid", {}, None,
        )

    monkeypatch.setattr(psi.urllib.request, "urlopen", _raise_http_error)
    result = psi.probe_provider("https://example.test", FIXTURE_MARKER)
    assert result.outcome == psi.PROBE_INVALID_KEY
    assert FIXTURE_MARKER not in result.detail
    captured = capsys.readouterr()
    assert FIXTURE_MARKER not in captured.out
    assert FIXTURE_MARKER not in captured.err


# ── Блокирующая 1 гейта PR #778: h.getresponse() кидает не-URLError ──────


def test_probe_provider_connection_reset_is_classified_not_raised(capsys):
    """Сервер принимает запрос и рвёт соединение RST'ом (SO_LINGER=0) —
    h.getresponse() бросает ConnectionResetError, urllib его НЕ оборачивает
    в URLError. Мутация: убери except (OSError, http.client.HTTPException) —
    этот тест упадёт необработанным исключением, а не просто покраснеет на
    assert."""
    def handler(conn: socket.socket) -> None:
        _read_request(conn)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    server = _RawTcpServer(handler)
    try:
        result = psi.probe_provider(server.base_url, FIXTURE_MARKER)
    finally:
        server.close()
    assert result.outcome == psi.PROBE_NETWORK_UNAVAILABLE
    assert FIXTURE_MARKER not in result.detail
    captured = capsys.readouterr()
    assert FIXTURE_MARKER not in captured.out
    assert FIXTURE_MARKER not in captured.err


def test_probe_provider_timeout_is_classified_not_raised(monkeypatch, capsys):
    """Сервер принимает запрос и молчит дольше таймаута пробы —
    h.getresponse() бросает TimeoutError, urllib его НЕ оборачивает в
    URLError. _PROBE_TIMEOUT_SECONDS уменьшен монкипатчем, чтобы тест не ждал
    боевые 10 секунд."""
    monkeypatch.setattr(psi, "_PROBE_TIMEOUT_SECONDS", 0.3)

    def handler(conn: socket.socket) -> None:
        _read_request(conn)
        time.sleep(1.0)

    server = _RawTcpServer(handler)
    try:
        result = psi.probe_provider(server.base_url, FIXTURE_MARKER)
    finally:
        server.close()
    assert result.outcome == psi.PROBE_NETWORK_UNAVAILABLE
    assert FIXTURE_MARKER not in result.detail
    captured = capsys.readouterr()
    assert FIXTURE_MARKER not in captured.out
    assert FIXTURE_MARKER not in captured.err


def test_probe_provider_bad_status_line_is_classified_not_raised(capsys):
    """Сервер отвечает мусором вместо статус-строки (echo маркера в мусоре,
    чтобы доказать, что даже если сервер отражает вход обратно,
    probe_provider не пересказывает это в detail/печати) —
    http.client.BadStatusLine, НЕ OSError, нужны оба класса в except."""
    def handler(conn: socket.socket) -> None:
        _read_request(conn)
        conn.sendall(f"GARBAGE {FIXTURE_MARKER} NOT-A-STATUS-LINE\r\n\r\n".encode())

    server = _RawTcpServer(handler)
    try:
        result = psi.probe_provider(server.base_url, FIXTURE_MARKER)
    finally:
        server.close()
    assert result.outcome == psi.PROBE_NETWORK_UNAVAILABLE
    assert FIXTURE_MARKER not in result.detail
    captured = capsys.readouterr()
    assert FIXTURE_MARKER not in captured.out
    assert FIXTURE_MARKER not in captured.err


def test_probe_candidates_survives_one_dead_gateway_among_many(dsh_ci_path, monkeypatch):
    """Регрессия ЭТОГО PR (блокирующая 1): один оборванный шлюз среди девяти
    не валит весь probe_candidates/main() — остальные кандидаты пробуются и
    классифицируются нормально.

    Блокирующая 1 гейта PR #781: раньше второе семейство (zai) оставалось с
    НАСТОЯЩИМ base_url боевого шлюза из dsh-ci.sh:59 — обязательный шаг CI слал
    третьей стороне живой HTTP-запрос с ключом-маркером в заголовке
    Authorization при каждом push/PR. Оба семейства теперь указывают на
    синтетические локальные серверы (мёртвый — openrouter, живой — zai) —
    никакого выхода в интернет, докстрин "остальные кандидаты пробуются и
    классифицируются нормально" доказан фактическим PROBE_ALIVE, а не
    угадан по len(results)."""
    monkeypatch.setattr(psi, "_PROBE_TIMEOUT_SECONDS", 0.3)
    routes = psi.parse_suite_routes(dsh_ci_path)
    routes_by_family = psi.group_routes_by_family(routes)

    def dead_handler(conn: socket.socket) -> None:
        _read_request(conn)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    def alive_handler(conn: socket.socket) -> None:
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")

    dead_server = _RawTcpServer(dead_handler)
    alive_server = _RawTcpServer(alive_handler)
    try:
        # Подменяем базовый URL первого маршрута openrouter на мёртвый сервер
        # и первого маршрута zai — на живой (оба синтетические, localhost).
        dead_route = routes_by_family["openrouter"][0]
        routes_by_family["openrouter"][0] = psi.SuiteRoute(
            alias=dead_route.alias, family=dead_route.family, slot=dead_route.slot,
            base_url=dead_server.base_url, secret_env=dead_route.secret_env,
            display_name=dead_route.display_name,
        )
        zai_route = routes_by_family["zai"][0]
        routes_by_family["zai"][0] = psi.SuiteRoute(
            alias=zai_route.alias, family=zai_route.family, slot=zai_route.slot,
            base_url=alive_server.base_url, secret_env=zai_route.secret_env,
            display_name=zai_route.display_name,
        )
        candidates_by_family = {
            "openrouter": _accounts_from_dicts([
                _account("openrouter", 1, True, "unavailable", f"{FIXTURE_MARKER}-or1"),
            ]),
            "zai": _accounts_from_dicts([
                _account("glm", 1, True, "unavailable", f"{FIXTURE_MARKER}-glm1"),
            ]),
        }
        # Трассировщик: доказывает, что probe_candidates не открывает НИ ОДНОГО
        # сетевого сокета вне двух заведённых здесь локальных серверов.
        opened_hosts: list[tuple[str, int]] = []
        real_create_connection = socket.create_connection

        def _tracing_create_connection(address, *args, **kwargs):
            opened_hosts.append(address)
            return real_create_connection(address, *args, **kwargs)

        monkeypatch.setattr(socket, "create_connection", _tracing_create_connection)
        results = psi.probe_candidates(candidates_by_family, routes_by_family)
    finally:
        dead_server.close()
        alive_server.close()
    assert len(results) == 2
    or_result = next(v for k, v in results.items() if k.startswith("openrouter"))
    zai_result = next(v for k, v in results.items() if k.startswith("glm"))
    assert or_result.outcome == psi.PROBE_NETWORK_UNAVAILABLE
    assert zai_result.outcome == psi.PROBE_ALIVE
    allowed_hosts = {("127.0.0.1", dead_server.port), ("127.0.0.1", alive_server.port)}
    assert opened_hosts, "трассировщик не увидел ни одного открытого сокета"
    assert set(opened_hosts) <= allowed_hosts, (
        f"probe_candidates открыл сокет за пределами локальных фикстур: {opened_hosts}"
    )


# ── parse_model_ids / probe_provider_full: печать id моделей, не тела ────
# Задача «импортёр печатает доступные id моделей» — формат подтверждён живым
# запросом ко всем четырём провайдерам suite (nvidia-nim, zai, ollama-cloud,
# openrouter, 2026-09-09): везде dict {"data": [{"id": ..., ...}, ...]}.


def test_parse_model_ids_openai_format():
    body = json.dumps({
        "object": "list",
        "data": [
            {"id": "vendor-model-b", "created": 1, "object": "model", "owned_by": "vendor"},
            {"id": "vendor-model-a", "created": 1, "object": "model", "owned_by": "vendor"},
        ],
    }).encode()
    assert psi.parse_model_ids("https://example.test/v4", body) == ["vendor-model-a", "vendor-model-b"]


def test_parse_model_ids_ignores_extra_fields_like_openrouter():
    """openrouter несёt лишние поля (pricing/context_length/…) — парсер
    использует только 'id', остальное не читает и не падает на него."""
    body = json.dumps({
        "data": [
            {"id": "amazon/nova-pro-v1", "pricing": {"prompt": "0.0008"}, "context_length": 300000},
        ],
        "links": {}, "total_count": 1,
    }).encode()
    assert psi.parse_model_ids("https://openrouter.ai/api/v1", body) == ["amazon/nova-pro-v1"]


def test_parse_model_ids_dedupes_and_sorts():
    body = json.dumps({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]}).encode()
    assert psi.parse_model_ids("https://example.test", body) == ["a", "b"]


def test_parse_model_ids_unrecognized_top_level_fails_loud_without_values():
    """Неузнанная структура — сообщение называет ТИП/КЛЮЧИ, не значения.
    Маркер вписан как ЗНАЧЕНИЕ (не как имя ключа) — не должен просочиться."""
    body = json.dumps({"unexpected_field": FIXTURE_MARKER, "another": 1}).encode()
    with pytest.raises(psi.ModelListError) as excinfo:
        psi.parse_model_ids("https://example.test", body)
    message = str(excinfo.value)
    assert FIXTURE_MARKER not in message
    assert "unexpected_field" in message  # имя ключа — это структура, не значение
    assert "dict" in message


def test_parse_model_ids_unrecognized_item_shape_fails_loud_without_values():
    body = json.dumps({"data": [{"weird_field": FIXTURE_MARKER}]}).encode()
    with pytest.raises(psi.ModelListError) as excinfo:
        psi.parse_model_ids("https://example.test", body)
    message = str(excinfo.value)
    assert FIXTURE_MARKER not in message
    assert "weird_field" in message


def test_parse_model_ids_not_json_fails_loud():
    with pytest.raises(psi.ModelListError):
        psi.parse_model_ids("https://example.test", b"not json at all")


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_probe_provider_full_success_extracts_models_without_leaking_key(monkeypatch):
    """Ответ несёт маркер как значение НЕ-'id' поля (гипотетическое эхо
    запроса в постороннем поле) — модели содержат только настоящие id,
    маркер нигде не всплывает."""
    body = json.dumps({
        "data": [
            {"id": "model-a", "description": FIXTURE_MARKER},
            {"id": "model-b"},
        ],
    }).encode()

    def _fake_urlopen(request, timeout=None):
        assert FIXTURE_MARKER not in request.full_url
        return _FakeResponse(200, body)

    monkeypatch.setattr(psi.urllib.request, "urlopen", _fake_urlopen)
    outcome = psi.probe_provider_full("https://example.test", FIXTURE_MARKER)
    assert outcome.outcome == psi.PROBE_ALIVE
    assert outcome.models == ("model-a", "model-b")
    assert outcome.models_error is None
    assert FIXTURE_MARKER not in outcome.models
    assert FIXTURE_MARKER not in (outcome.models_error or "")


def test_probe_provider_full_malformed_body_error_names_structure_not_values(monkeypatch):
    body = json.dumps({"weird_top_level": FIXTURE_MARKER}).encode()

    def _fake_urlopen(request, timeout=None):
        return _FakeResponse(200, body)

    monkeypatch.setattr(psi.urllib.request, "urlopen", _fake_urlopen)
    outcome = psi.probe_provider_full("https://example.test", FIXTURE_MARKER)
    assert outcome.outcome == psi.PROBE_ALIVE  # HTTP 200 — статус жив, модели просто не разобрались
    assert outcome.models is None
    assert outcome.models_error is not None
    assert FIXTURE_MARKER not in outcome.models_error
    assert "weird_top_level" in outcome.models_error


def test_probe_provider_full_never_leaks_key_or_auth_header_on_success(monkeypatch):
    """Request собирается из base_url/api_key — сам ключ никогда не читается
    обратно из request в вывод (ProbeResult не хранит ни headers, ни ключ)."""
    body = json.dumps({"data": [{"id": "m"}]}).encode()
    captured_requests: list = []

    def _fake_urlopen(request, timeout=None):
        captured_requests.append(request)
        return _FakeResponse(200, body)

    monkeypatch.setattr(psi.urllib.request, "urlopen", _fake_urlopen)
    outcome = psi.probe_provider_full("https://example.test", FIXTURE_MARKER)
    assert captured_requests[0].get_header("Authorization") == f"Bearer {FIXTURE_MARKER}"
    # ProbeResult — единственное, что уходит наружу из этой функции.
    rendered = f"{outcome.outcome} {outcome.detail} {outcome.models} {outcome.models_error}"
    assert FIXTURE_MARKER not in rendered


# ── Ранг: квота (429/backoff) — НЕ дисквалификация, ключ неверен — да ────
# Задача #777, критерий 7 — все четыре сценария по мутации: закомментируй
# приоритет tier снимка/пробы в rank_account/classify_* — эти тесты краснеют.


def _accounts_from_dicts(dicts):
    return psi.accounts_from_export({"providerConnections": dicts})


def test_classify_snapshot_quota_not_worse_than_unknown_but_not_disqualified():
    healthy = _accounts_from_dicts([
        _account("openrouter", 1, True, "active", "k1"),
    ])[0]
    quota = _accounts_from_dicts([
        _account("openrouter", 2, False, "unavailable", "k2", error_code=429),
    ])[0]
    invalid_key = _accounts_from_dicts([
        _account("openrouter", 3, False, "unavailable", "k3", error_code=401),
    ])[0]
    tier_healthy, _ = psi.classify_snapshot(healthy)
    tier_quota, note_quota = psi.classify_snapshot(quota)
    tier_invalid, note_invalid = psi.classify_snapshot(invalid_key)
    assert tier_healthy < tier_quota < tier_invalid
    assert "дисквалификация" in note_quota or "не дисквалификация" in note_quota
    assert "ключ неверен" in note_invalid


def test_live_probe_alive_beats_snapshot_active_probed_401(dsh_ci_path):
    """isActive=False/testStatus=unavailable, но живая проба 200 — обязана
    подняться выше учётки с testStatus=active, чья живая проба даёт 401."""
    routes = psi.parse_suite_routes(dsh_ci_path)
    stale_but_alive, confirmed_dead = _accounts_from_dicts([
        _account("openrouter", 1, False, "unavailable", "k-stale", error_code=429),
        _account("openrouter", 2, True, "active", "k-active"),
    ])
    probe_results = {
        stale_but_alive.id: psi.ProbeResult(psi.PROBE_ALIVE, "HTTP 200"),
        confirmed_dead.id: psi.ProbeResult(psi.PROBE_INVALID_KEY, "HTTP 401"),
    }
    selection = psi.select_accounts(
        [stale_but_alive, confirmed_dead], routes, probe_results=probe_results,
    )
    by_secret = {a.route.secret_env: a for a in selection.assignments}
    assert by_secret["OPENROUTER_1_API_KEY"].account.api_key == "k-stale"
    # confirmed_dead исключён живой пробой из слота, не занимает второй слот молча.
    assert by_secret["OPENROUTER_2_API_KEY"].account is None
    assert by_secret["OPENROUTER_2_API_KEY"].empty_reason is not None
    assert "ключ неверен" in by_secret["OPENROUTER_2_API_KEY"].empty_reason


def test_live_probe_quota_beats_live_probe_invalid_key(dsh_ci_path):
    routes = psi.parse_suite_routes(dsh_ci_path)
    quota_acc, dead_acc = _accounts_from_dicts([
        _account("openrouter", 1, True, "unavailable", "k-quota"),
        _account("openrouter", 2, True, "unavailable", "k-dead"),
    ])
    probe_results = {
        quota_acc.id: psi.ProbeResult(psi.PROBE_QUOTA, "HTTP 429"),
        dead_acc.id: psi.ProbeResult(psi.PROBE_INVALID_KEY, "HTTP 401"),
    }
    selection = psi.select_accounts([quota_acc, dead_acc], routes, probe_results=probe_results)
    by_secret = {a.route.secret_env: a for a in selection.assignments}
    assert by_secret["OPENROUTER_1_API_KEY"].account.api_key == "k-quota"
    assert by_secret["OPENROUTER_2_API_KEY"].account is None
    assert dead_acc in selection.probe_excluded


def test_probe_unreachable_falls_back_to_snapshot_and_says_so(dsh_ci_path):
    routes = psi.parse_suite_routes(dsh_ci_path)
    (acc,) = _accounts_from_dicts([
        _account("openrouter", 1, True, "active", "k1"),
    ])
    probe_results = {acc.id: psi.ProbeResult(psi.PROBE_NETWORK_UNAVAILABLE, "сеть: [Errno -2] Name or service not known")}
    selection = psi.select_accounts([acc], routes, probe_results=probe_results)
    tier, note, source = selection.rank_by_id[acc.id]
    assert source == "снимок"
    assert "проба недоступна по сети" in note
    assert tier == psi._TIER_HEALTHY


def test_all_candidates_dead_by_probe_leaves_slot_empty_with_reason(dsh_ci_path):
    routes = psi.parse_suite_routes(dsh_ci_path)
    (glm_acc,) = _accounts_from_dicts([
        _account("glm", 1, False, "unavailable", "k-dead-glm"),
    ])
    probe_results = {glm_acc.id: psi.ProbeResult(psi.PROBE_INVALID_KEY, "HTTP 401")}
    selection = psi.select_accounts([glm_acc], routes, probe_results=probe_results)
    by_secret = {a.route.secret_env: a for a in selection.assignments}
    assert by_secret["ZAI_1_API_KEY"].account is None
    assert by_secret["ZAI_1_API_KEY"].empty_reason is not None
    assert "1 кандидат" in by_secret["ZAI_1_API_KEY"].empty_reason
    assert "ключ неверен" in by_secret["ZAI_1_API_KEY"].empty_reason
    assert glm_acc in selection.probe_excluded
    assert len(selection.overflow) == 0


# ── Minor 7 гейта PR #778: снимок называет код, не сливает «есть, но не ────
# 401/403/429» и «кода нет вовсе» в одну строку ─────────────────────────


def test_classify_snapshot_distinguishes_unknown_code_from_missing_code():
    unknown_code = _accounts_from_dicts([
        _account("openrouter", 1, False, "unavailable", "k1", error_code=503),
    ])[0]
    missing_code = _accounts_from_dicts([
        _account("openrouter", 2, False, "unavailable", "k2", error_code=None),
    ])[0]
    tier_unknown, note_unknown = psi.classify_snapshot(unknown_code)
    tier_missing, note_missing = psi.classify_snapshot(missing_code)
    assert tier_unknown == psi._TIER_UNKNOWN == tier_missing
    assert "503" in note_unknown
    assert "errorCode отсутствует" in note_missing
    assert "503" not in note_missing


def test_dead_by_probe_reason_only_on_first_empty_slot_not_every_empty_slot(dsh_ci_path):
    """Minor 6 гейта PR #778: 2 слота nvidia-nim, 1 кандидат — и тот выброшен
    живой пробой (ключ неверен). Пустых слотов 2, выброшенных пробой — 1:
    причина «исключён пробой» обязана достаться ТОЛЬКО первому пустому
    слоту, второй — «нет кандидата» (иначе читатель решит, что починка
    одного ключа заполнит оба слота)."""
    routes = psi.parse_suite_routes(dsh_ci_path)
    (dead_acc,) = _accounts_from_dicts([
        _account("nvidia", 1, False, "unavailable", "k-dead-nv"),
    ])
    probe_results = {dead_acc.id: psi.ProbeResult(psi.PROBE_INVALID_KEY, "HTTP 401")}
    selection = psi.select_accounts([dead_acc], routes, probe_results=probe_results)
    by_secret = {a.route.secret_env: a for a in selection.assignments}
    assert by_secret["NVIDIA_NIM_1_API_KEY"].account is None
    assert by_secret["NVIDIA_NIM_1_API_KEY"].empty_reason is not None
    assert "ключ неверен" in by_secret["NVIDIA_NIM_1_API_KEY"].empty_reason
    assert by_secret["NVIDIA_NIM_2_API_KEY"].account is None
    assert by_secret["NVIDIA_NIM_2_API_KEY"].empty_reason is None


def test_dead_by_probe_reason_not_duplicated_across_multiple_empty_slots(dsh_ci_path):
    """Minor 3 гейта PR #781: 2 слота nvidia-nim, ОБА кандидата выброшены
    живой пробой — 2 пустых слота, 2 выброшенных. Раньше
    empty_reason_budget = len(dead_by_probe) отдавал ОДНУ И ТУ ЖЕ агрегатную
    строку («2 кандидат(ов) исключены...») ОБОИМ пустым слотам — читатель,
    суммирующий число исключённых по числу строк, получил бы 4 исключённых
    при фактических 2-х. Мутация: верни `empty_reason_budget =
    len(dead_by_probe)` — этот тест покраснеет на втором слоте."""
    routes = psi.parse_suite_routes(dsh_ci_path)
    dead_acc_1, dead_acc_2 = _accounts_from_dicts([
        _account("nvidia", 1, False, "unavailable", "k-dead-nv1"),
        _account("nvidia", 2, False, "unavailable", "k-dead-nv2"),
    ])
    probe_results = {
        dead_acc_1.id: psi.ProbeResult(psi.PROBE_INVALID_KEY, "HTTP 401"),
        dead_acc_2.id: psi.ProbeResult(psi.PROBE_INVALID_KEY, "HTTP 401"),
    }
    selection = psi.select_accounts(
        [dead_acc_1, dead_acc_2], routes, probe_results=probe_results,
    )
    by_secret = {a.route.secret_env: a for a in selection.assignments}
    assert by_secret["NVIDIA_NIM_1_API_KEY"].account is None
    assert by_secret["NVIDIA_NIM_1_API_KEY"].empty_reason is not None
    assert "2 кандидат" in by_secret["NVIDIA_NIM_1_API_KEY"].empty_reason
    # Только ПЕРВЫЙ пустой слот несёт причину — второй не повторяет её.
    assert by_secret["NVIDIA_NIM_2_API_KEY"].account is None
    assert by_secret["NVIDIA_NIM_2_API_KEY"].empty_reason is None


# ── Блокирующая 3 гейта PR #778: снимочная дисквалификация НЕ выбрасывает ─


def test_snapshot_disqualified_without_probe_still_fills_slot(dsh_ci_path):
    """Снимок с errorCode=401, единственный кандидат, живой пробы НЕ было
    (probe_results={}) — слот ЗАНЯТ, probe_excluded пуст, у слота нет
    empty_reason. Мутация :434 гейта PR #778 (снять условие «источник —
    проба» из проверки дисквалификации) красит этот тест: без условия слот
    опустеет и получит ложную причину «исключён живой пробой», хотя пробы
    не было вовсе — регрессия ровно в дефект, ради которого заведена #777."""
    routes = psi.parse_suite_routes(dsh_ci_path)
    (only_candidate,) = _accounts_from_dicts([
        _account("glm", 1, False, "unavailable", "k-only", error_code=401),
    ])
    selection = psi.select_accounts([only_candidate], routes, probe_results={})
    by_secret = {a.route.secret_env: a for a in selection.assignments}
    assert by_secret["ZAI_1_API_KEY"].account is only_candidate
    assert by_secret["ZAI_1_API_KEY"].empty_reason is None
    assert selection.probe_excluded == []


# ── Major 5 гейта PR #778: 403 — отдельный тир, БЕЗ выброса из слота ──────


def test_probe_provider_403_is_suspect_not_invalid_key(monkeypatch):
    """probe_provider сам обязан различать 401 и 403 (не только classify_*
    на готовом ProbeResult) — иначе тесты выше проверяли бы контракт
    classify_probe/classify_snapshot, но не сам probe_provider."""
    import urllib.error

    def _raise_403(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://example.test/models", 403, "forbidden", {}, None,
        )

    monkeypatch.setattr(psi.urllib.request, "urlopen", _raise_403)
    result = psi.probe_provider("https://example.test", FIXTURE_MARKER)
    assert result.outcome == psi.PROBE_SUSPECT_FORBIDDEN
    assert result.outcome != psi.PROBE_INVALID_KEY


def test_classify_snapshot_403_is_suspect_not_disqualified():
    acc = _accounts_from_dicts([
        _account("openrouter", 1, False, "unavailable", "k1", error_code=403),
    ])[0]
    tier, note = psi.classify_snapshot(acc)
    assert tier == psi._TIER_SUSPECT
    assert tier != psi._TIER_DISQUALIFIED
    assert "403" in note


def test_live_probe_403_ranks_worse_than_quota_but_does_not_exclude(dsh_ci_path):
    """403 хуже квоты по рангу, но НЕ одноразово надёжен как факт
    дисквалификации (гео-блок/WAF/лимит плана при годном ключе) — слот НЕ
    пустеет, кандидат с 403 занимает второй слот, а не первый."""
    routes = psi.parse_suite_routes(dsh_ci_path)
    quota_acc, suspect_acc = _accounts_from_dicts([
        _account("openrouter", 1, True, "unavailable", "k-quota"),
        _account("openrouter", 2, True, "unavailable", "k-403"),
    ])
    probe_results = {
        quota_acc.id: psi.ProbeResult(psi.PROBE_QUOTA, "HTTP 429"),
        suspect_acc.id: psi.ProbeResult(psi.PROBE_SUSPECT_FORBIDDEN, "HTTP 403"),
    }
    selection = psi.select_accounts([quota_acc, suspect_acc], routes, probe_results=probe_results)
    by_secret = {a.route.secret_env: a for a in selection.assignments}
    assert by_secret["OPENROUTER_1_API_KEY"].account.api_key == "k-quota"
    assert by_secret["OPENROUTER_2_API_KEY"].account.api_key == "k-403"
    assert suspect_acc not in selection.probe_excluded


def test_render_report_names_gas_for_suspect_403_slot(dsh_ci_path):
    """Minor 4 гейта PR #781: TIER_SUSPECT (403) занимает слот молча — текст
    кончался перечнем гипотез без ответа на «что делать». Отчёт обязан
    называть газ: что сделать, если 403 устойчив."""
    routes = psi.parse_suite_routes(dsh_ci_path)
    (suspect_acc,) = _accounts_from_dicts([
        _account("openrouter", 1, True, "unavailable", "k-403"),
    ])
    probe_results = {suspect_acc.id: psi.ProbeResult(psi.PROBE_SUSPECT_FORBIDDEN, "HTTP 403")}
    selection = psi.select_accounts([suspect_acc], routes, probe_results=probe_results)
    report = psi.render_report(
        selection, {"OPENROUTER_1_API_KEY": "создан"}, "не проверялась", True, "дата",
        no_probe=False, probe_results=probe_results, existing_secrets=set(),
        repo="owner/repo",
    )
    lines_by_secret = {}
    for line in report.splitlines():
        if not line.startswith("| ") or "|---|" in line:
            continue
        first_cell = line.split("|")[1].strip()
        if first_cell == "секрет":
            continue
        lines_by_secret[first_cell] = line
    assert "Газ при устойчивом 403" in lines_by_secret["OPENROUTER_1_API_KEY"]


def test_render_report_shows_export_date_and_rank_source(monkeypatch, export_path, dsh_ci_path):
    data = psi.load_export(str(export_path))
    routes = psi.parse_suite_routes(dsh_ci_path)
    accounts = psi.accounts_from_export(data)
    selection = psi.select_accounts(accounts, routes)
    report = psi.render_report(
        selection, {}, "не проверялась", False, "2026-08-25 (дата из имени файла)",
        no_probe=True, probe_results={}, existing_secrets=set(), repo="owner/repo",
    )
    assert "2026-08-25" in report
    assert "ПРЕДОХРАНИТЕЛЯ" in report
    assert "источник: снимок" in report


def test_render_report_shows_models_section_by_secret(monkeypatch, export_path, dsh_ci_path):
    data = psi.load_export(str(export_path))
    routes = psi.parse_suite_routes(dsh_ci_path)
    accounts = psi.accounts_from_export(data)
    probe_results = {
        a.id: psi.ProbeResult(psi.PROBE_ALIVE, "HTTP 200", models=("model-x", "model-y"))
        for a in accounts if a.provider in ("openrouter", "nvidia", "glm")
    }
    selection = psi.select_accounts(accounts, routes, probe_results=probe_results)
    report = psi.render_report(
        selection, {}, "не проверялась", False, "2026-08-25 (дата из имени файла)",
        no_probe=False, probe_results=probe_results, existing_secrets=set(), repo="owner/repo",
    )
    assert "Доступные id моделей" in report
    assert "NVIDIA_NIM_1_API_KEY" in report
    assert "model-x" in report and "model-y" in report
    for account in accounts:
        if account.api_key:
            assert account.api_key not in report


# ── Major 4 гейта PR #778: пустой слот с уже существующим секретом ───────


def test_render_report_distinguishes_empty_slot_with_existing_secret(dsh_ci_path):
    routes = psi.parse_suite_routes(dsh_ci_path)
    selection = psi.select_accounts([], routes, probe_results={})
    report = psi.render_report(
        selection, {}, "не проверялась", False, "дата", no_probe=True,
        probe_results={}, existing_secrets={"OPENROUTER_1_API_KEY"},
        # Minor 6 гейта PR #781: repo — не литерал-плейсхолдер, реальный
        # owner/repo — команда газа обязана копипаститься без правки.
        repo="acme-corp/edge-harness",
    )
    lines_by_secret = {}
    for line in report.splitlines():
        if not line.startswith("| ") or "|---|" in line:
            continue
        first_cell = line.split("|")[1].strip()
        if first_cell == "секрет":  # заголовок таблицы, не строка слота
            continue
        lines_by_secret[first_cell] = line
    assert "НЕ будет удалён" in lines_by_secret["OPENROUTER_1_API_KEY"]
    assert "gh secret delete" in lines_by_secret["OPENROUTER_1_API_KEY"]
    assert "--repo acme-corp/edge-harness" in lines_by_secret["OPENROUTER_1_API_KEY"]
    assert "<owner/repo>" not in lines_by_secret["OPENROUTER_1_API_KEY"]
    # Слот без существующего секрета — формулировка другая, без газа удаления.
    assert "нет кандидата" in lines_by_secret["OPENROUTER_2_API_KEY"]
    assert "НЕ будет удалён" not in lines_by_secret["OPENROUTER_2_API_KEY"]


# ── Minor 8 гейта PR #778: отчёт называет факт режима пробы, не гадает ───


def test_render_report_names_probe_mode_as_fact_not_guess():
    routes_fixture_selection = psi.SelectionResult(
        assignments=[], overflow=[], out_of_scope=[], probe_excluded=[], rank_by_id={},
    )
    report_no_probe = psi.render_report(
        routes_fixture_selection, {}, "не проверялась", False, "дата",
        no_probe=True, probe_results={}, existing_secrets=set(), repo="owner/repo",
    )
    assert "--no-probe" in report_no_probe

    probe_results = {
        "a": psi.ProbeResult(psi.PROBE_ALIVE, "HTTP 200"),
        "b": psi.ProbeResult(psi.PROBE_QUOTA, "HTTP 429"),
        "c": psi.ProbeResult(psi.PROBE_INVALID_KEY, "HTTP 401"),
    }
    report_with_probe = psi.render_report(
        routes_fixture_selection, {}, "не проверялась", False, "дата",
        no_probe=False, probe_results=probe_results, existing_secrets=set(), repo="owner/repo",
    )
    assert "3 кандидат" in report_with_probe
    assert "жива 1" in report_with_probe
    assert "квота 1" in report_with_probe
    assert "ключ неверен 1" in report_with_probe


# ── Блокирующая 2 гейта PR #781: разбивка пробы — одно место правды ──────


def test_render_report_probe_breakdown_covers_every_outcome_and_sums_to_total():
    """Раньше `order` в render_report был ВТОРОЙ копией множества исходов
    (первая — _PROBE_BUCKET_LABELS), и исход, присутствующий в
    _PROBE_BUCKET_LABELS, но выпавший из литерала `order`, молча пропадал из
    разбивки, хотя тотал (len(probe_results)) его всё равно считал. Мутация:
    выкинь любую строку из `order` (верни литеральную копию списка) — этот
    тест покраснеет, потому что каждая метка из _PROBE_BUCKET_LABELS обязана
    появиться в отчёте ровно один раз, и сумма чисел разбивки обязана
    сойтись с тоталом."""
    routes_fixture_selection = psi.SelectionResult(
        assignments=[], overflow=[], out_of_scope=[], probe_excluded=[], rank_by_id={},
    )
    outcomes = list(psi._PROBE_BUCKET_LABELS)
    probe_results = {
        f"acct-{i}": psi.ProbeResult(outcome, "detail")
        for i, outcome in enumerate(outcomes)
    }
    report = psi.render_report(
        routes_fixture_selection, {}, "не проверялась", False, "дата",
        no_probe=False, probe_results=probe_results, existing_secrets=set(), repo="owner/repo",
    )
    assert f"{len(outcomes)} кандидат" in report
    total_in_breakdown = 0
    for label in psi._PROBE_BUCKET_LABELS.values():
        assert f"{label} 1" in report, f"метка {label!r} пропала из разбивки пробы"
        total_in_breakdown += 1
    assert total_in_breakdown == len(probe_results)


# ── argv без значений (доказано мутацией на уровне вызова gh) ────────────


def test_set_secret_value_never_in_argv(monkeypatch):
    calls = []

    class FakeCompleted:
        returncode = 0
        stderr = ""

    def fake_run(args, input=None, text=None, capture_output=None):
        calls.append((list(args), input))
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    psi.set_secret("owner/repo", "SOME_API_KEY", FIXTURE_MARKER)

    assert len(calls) == 1
    args, stdin_value = calls[0]
    assert FIXTURE_MARKER not in args
    assert stdin_value == FIXTURE_MARKER
    assert "--body-file" in args and "-" in args


# ── export_snapshot_date: дата снимка предохранителя ─────────────────────


def test_export_snapshot_date_from_filename(tmp_path):
    path = tmp_path / "some-export-2026-08-25T12-48-15-414Z.json"
    path.write_text("{}", encoding="utf-8")
    assert "2026-08-25" in psi.export_snapshot_date(path)
    assert "имени файла" in psi.export_snapshot_date(path)


def test_export_snapshot_date_falls_back_to_mtime(tmp_path):
    path = tmp_path / "export-without-date.json"
    path.write_text("{}", encoding="utf-8")
    result = psi.export_snapshot_date(path)
    assert "mtime" in result


# ── Идемпотентность: существующий секрет не перезаписывается без флага ──


def test_idempotent_skip_existing_secret_without_force(monkeypatch, export_path, dsh_ci_path, capsys):
    monkeypatch.setattr(psi, "gh_repo", lambda explicit: "owner/repo")
    monkeypatch.setattr(psi, "existing_secret_names", lambda repo: {"OPENROUTER_1_API_KEY"})
    monkeypatch.setattr(psi, "existing_variable_names", lambda repo: set())

    set_secret_calls = []
    monkeypatch.setattr(psi, "set_secret", lambda repo, name, value: set_secret_calls.append(name))
    monkeypatch.setattr(psi, "set_variable", lambda repo, name, value: None)
    monkeypatch.setattr(
        psi, "probe_provider_full",
        lambda base_url, value: psi.ProbeResult(psi.PROBE_ALIVE, "HTTP 200"),
    )

    rc = psi.main([
        "--export-file", str(export_path),
        "--dsh-ci-path", str(dsh_ci_path),
        "--apply",
    ])
    assert rc == 0
    assert "OPENROUTER_1_API_KEY" not in set_secret_calls

    rc = psi.main([
        "--export-file", str(export_path),
        "--dsh-ci-path", str(dsh_ci_path),
        "--apply",
        "--force-secrets",
    ])
    assert rc == 0
    assert "OPENROUTER_1_API_KEY" in set_secret_calls
