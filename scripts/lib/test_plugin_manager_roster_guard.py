#!/usr/bin/env python3
"""Тесты гвардии класса #806 (scripts/lib/plugin_manager_roster_guard.py).

Мутация-доказательство (описана в теле PR, не автоматизирована здесь —
сама гвардия и есть код под тестом): `git stash` строки `if plugin_manager_
source(old_manifest) != plugin_manager_source(new_manifest): return None` в
plugin_manager_roster_guard.py делает test_real_incident_0a85ddf5_is_flagged
и test_roster_change_without_manager_bump_is_violation зелёными даже без
плагина-виновника (тривиально) — обратная мутация (закомментировать первый
`if` целиком, то есть перестать сверять ростер) красит их: без сверки
функция вернула бы None всегда, что и есть предмет доказательства (тест ловит
регресс).

Запуск: python -m pytest scripts/lib/test_plugin_manager_roster_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import base64
import importlib
import json
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
guard = importlib.import_module("plugin_manager_roster_guard")

# Живой инцидент #806/#412: коммит 0a85ddf5 добавил agents-tasks в
# dsh-edge/plugins.json без пересборки plugin-manager.
INCIDENT_COMMIT = "0a85ddf5"


def _manifest_at(revision: str) -> dict:
    """Реальное содержимое dsh-edge/plugins.json на заданной ревизии этого же
    репозитория (прод-форма, не пересказ) — `git show`, офлайн, без сети."""
    result = subprocess.run(
        ["git", "show", f"{revision}:dsh-edge/plugins.json"],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return json.loads(result.stdout)


def test_roster_projection_keeps_order_and_only_named_fields():
    manifest = {"plugins": [
        {"id": "a", "package": "@x/a", "server": True, "client": False, "source": {"sha256": "x"}},
        {"id": "b", "package": "@x/b", "server": False, "client": True},
    ]}
    assert guard.roster_projection(manifest) == [
        {"id": "a", "server": True, "client": False},
        {"id": "b", "server": False, "client": True},
    ]


def test_plugin_manager_source_found_and_missing():
    manifest = {"plugins": [
        {"id": "plugin-manager", "package": guard.PLUGIN_MANAGER_PACKAGE, "source": {"release": "r1"}},
        {"id": "other", "package": "@x/other", "source": {"release": "r2"}},
    ]}
    assert guard.plugin_manager_source(manifest) == {"release": "r1"}
    assert guard.plugin_manager_source({"plugins": []}) is None


def test_no_roster_change_is_never_a_violation():
    manifest = {"plugins": [
        {"id": "a", "package": guard.PLUGIN_MANAGER_PACKAGE, "server": False, "client": True, "source": {"release": "r1"}},
    ]}
    assert guard.roster_bump_violation(manifest, json.loads(json.dumps(manifest))) is None


def test_roster_change_without_manager_bump_is_violation():
    old = {"plugins": [
        {"id": "hello", "package": "@x/hello", "server": True, "client": True},
        {"id": "plugin-manager", "package": guard.PLUGIN_MANAGER_PACKAGE, "server": False, "client": True,
         "source": {"release": "plugins-plugin-manager-v0.1.9", "asset": "plugin-manager-0.1.9.tgz", "sha256": "a" * 64}},
    ]}
    new = json.loads(json.dumps(old))
    new["plugins"].append({"id": "agents-tasks", "package": "@x/agents-tasks", "server": False, "client": True})
    violation = guard.roster_bump_violation(old, new)
    assert violation is not None
    assert guard.PLUGIN_MANAGER_PACKAGE in violation
    assert "#806" in violation


def test_roster_change_with_manager_bump_is_clean():
    old = {"plugins": [
        {"id": "plugin-manager", "package": guard.PLUGIN_MANAGER_PACKAGE, "server": False, "client": True,
         "source": {"release": "plugins-plugin-manager-v0.1.9", "asset": "plugin-manager-0.1.9.tgz", "sha256": "a" * 64}},
    ]}
    new = json.loads(json.dumps(old))
    new["plugins"].append({"id": "agents-tasks", "package": "@x/agents-tasks", "server": False, "client": True})
    new["plugins"][0]["source"] = {"release": "plugins-plugin-manager-v0.1.10", "asset": "plugin-manager-0.1.10.tgz", "sha256": "b" * 64}
    assert guard.roster_bump_violation(old, new) is None


def test_real_incident_0a85ddf5_is_flagged():
    """Прод-форма данных: реальный dsh-edge/plugins.json до/после коммита,
    который и вызвал issue #806 — гвардия обязана была бы покраснеть тогда."""
    old_manifest = _manifest_at(f"{INCIDENT_COMMIT}^")
    new_manifest = _manifest_at(INCIDENT_COMMIT)
    assert len(old_manifest["plugins"]) == 5
    assert len(new_manifest["plugins"]) == 6
    violation = guard.roster_bump_violation(old_manifest, new_manifest)
    assert violation is not None, "живой коммит 0a85ddf5 обязан детектироваться как класс #806"


def test_fetch_base_manifest_decodes_gh_contents_response(monkeypatch):
    payload = {"plugins": []}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["gh", "api"]
        return subprocess.CompletedProcess(cmd, 0, stdout=encoded + "\n", stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert guard.fetch_base_manifest("owner/repo", "deadbeef") == payload


def test_fetch_base_manifest_returns_none_on_404(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh: Not Found (HTTP 404)")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert guard.fetch_base_manifest("owner/repo", "deadbeef") is None


def test_fetch_base_manifest_raises_loud_on_other_failures(monkeypatch):
    """Fail loud (AGENTS.md): сбой gh, не являющийся 404, не имеет права
    молча читаться как «файла не было» — иначе сетевой сбой тихо гасит
    проверку класса #806 ровно там, где она нужнее всего."""
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh: rate limit exceeded")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        guard.fetch_base_manifest("owner/repo", "deadbeef")


def test_main_is_noop_outside_pull_request(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    assert guard.main() == 0
    assert "пропуск" in capsys.readouterr().out


def test_main_fails_the_pr_on_violation(monkeypatch, tmp_path, capsys):
    old_manifest = {"plugins": [
        {"id": "plugin-manager", "package": guard.PLUGIN_MANAGER_PACKAGE, "server": False, "client": True,
         "source": {"release": "plugins-plugin-manager-v0.1.9", "asset": "plugin-manager-0.1.9.tgz", "sha256": "a" * 64}},
    ]}
    new_manifest = json.loads(json.dumps(old_manifest))
    new_manifest["plugins"].append({"id": "agents-tasks", "package": "@x/agents-tasks", "server": False, "client": True})

    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"base": {"sha": "deadbeef"}}}), encoding="utf-8")
    manifest_path = tmp_path / "plugins.json"
    manifest_path.write_text(json.dumps(new_manifest), encoding="utf-8")

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(guard, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(guard, "fetch_base_manifest", lambda repo, sha: old_manifest)

    assert guard.main() == 1
    assert "::error::" in capsys.readouterr().out


def test_main_passes_when_no_drift(monkeypatch, tmp_path):
    manifest = {"plugins": [{"id": "a", "package": "@x/a", "server": True, "client": False}]}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps({"pull_request": {"base": {"sha": "deadbeef"}}}), encoding="utf-8")
    manifest_path = tmp_path / "plugins.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(guard, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(guard, "fetch_base_manifest", lambda repo, sha: manifest)

    assert guard.main() == 0
