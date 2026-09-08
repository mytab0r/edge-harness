"""Гвардия scripts/lib/provider_secrets_import.py (задача #733) — СИНТЕТИЧЕСКИЕ
фикстуры, никаких настоящих кредов. Проверяет: разбор контракта имён (PR #732)
из dsh-ci.sh, разбор формата экспорта, отказ при файле внутри рабочего дерева,
отсутствие значений в выводе (маркер фикстуры не встречается нигде), выбор
слотов по правилу isActive/testStatus/priority, идемпотентность, argv без
значений секрета.

Запуск: python -m pytest scripts/lib/test_provider_secrets_import.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provider_secrets_import as psi  # noqa: E402

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


def _account(provider, priority, is_active, test_status, api_key, email="acct@example.test"):
    return {
        "id": f"{provider}-{priority}",
        "provider": provider,
        "authType": "apikey",
        "email": email,
        "priority": priority,
        "isActive": is_active,
        "testStatus": test_status,
        "backoffLevel": 0,
        "lastError": "",
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
    monkeypatch.setattr(psi, "gh_repo", lambda explicit: "owner/repo")
    monkeypatch.setattr(psi, "existing_secret_names", lambda repo: set())
    monkeypatch.setattr(psi, "existing_variable_names", lambda repo: set())

    def _forbidden(*args, **kwargs):
        raise AssertionError("сухой прогон не должен звать set_secret/set_variable/probe_provider")

    monkeypatch.setattr(psi, "set_secret", _forbidden)
    monkeypatch.setattr(psi, "set_variable", _forbidden)
    monkeypatch.setattr(psi, "probe_provider", _forbidden)

    rc = psi.main([
        "--export-file", str(export_path),
        "--dsh-ci-path", str(dsh_ci_path),
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert FIXTURE_MARKER not in captured.out
    assert FIXTURE_MARKER not in captured.err
    # Ничего не записано ни в один файл — только в tmp_path созданное самим тестом.
    for path in export_path.parent.rglob("*"):
        if path in (export_path, dsh_ci_path) or path.is_dir():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert FIXTURE_MARKER not in content


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


# ── Идемпотентность: существующий секрет не перезаписывается без флага ──


def test_idempotent_skip_existing_secret_without_force(monkeypatch, export_path, dsh_ci_path, capsys):
    monkeypatch.setattr(psi, "gh_repo", lambda explicit: "owner/repo")
    monkeypatch.setattr(psi, "existing_secret_names", lambda repo: {"OPENROUTER_1_API_KEY"})
    monkeypatch.setattr(psi, "existing_variable_names", lambda repo: set())

    set_secret_calls = []
    monkeypatch.setattr(psi, "set_secret", lambda repo, name, value: set_secret_calls.append(name))
    monkeypatch.setattr(psi, "set_variable", lambda repo, name, value: None)
    monkeypatch.setattr(psi, "probe_provider", lambda base_url, value: "жива")

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
