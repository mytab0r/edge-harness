#!/usr/bin/env python3
"""Тесты rate_guard.py (#454) — гейт квоты перед дорогими job'ами.

Фикстура REAL_RATE_LIMIT_RESPONSE — дословный вывод `gh api rate_limit`,
захваченный живым вызовом 2026-09-06 (не пересказ формата, прод-форма,
AGENTS.md «тест кормит прод-форму данных»).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rate_guard as rg  # noqa: E402

# Дословный `gh api rate_limit`, живой вызов 2026-09-06 (PAT с полным лимитом
# 5000 — форма ответа для GITHUB_TOKEN идентична, только limit/remaining
# другие числа: 1000/час, см. docs/research/21-github-actions.md).
REAL_RATE_LIMIT_RESPONSE = json.dumps({
    "resources": {
        "core": {"limit": 5000, "used": 0, "remaining": 5000, "reset": 1788676450},
        "search": {"limit": 30, "used": 0, "remaining": 30, "reset": 1788672910},
        "graphql": {"limit": 5000, "used": 0, "remaining": 5000, "reset": 1788676450},
    },
    "rate": {"limit": 5000, "used": 0, "remaining": 5000, "reset": 1788676450},
})


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_gh(monkeypatch, *, stdout="", returncode=0, stderr=""):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(rg.subprocess, "run", fake_run)
    return calls


# ── fetch_core: разбор прод-формы ответа ─────────────────────────────────────


def test_fetch_core_parses_real_rate_limit_shape(monkeypatch):
    calls = _patch_gh(monkeypatch, stdout=REAL_RATE_LIMIT_RESPONSE)

    core = rg.fetch_core()

    assert core == {"limit": 5000, "used": 0, "remaining": 5000, "reset": 1788676450}
    assert calls == [["gh", "api", "rate_limit"]]


def test_fetch_core_raises_quota_check_failed_on_nonzero_exit(monkeypatch):
    _patch_gh(monkeypatch, returncode=1, stderr="HTTP 502: Bad Gateway")

    with pytest.raises(rg.QuotaCheckFailed, match="HTTP 502"):
        rg.fetch_core()


def test_fetch_core_raises_quota_check_failed_on_garbage_json(monkeypatch):
    _patch_gh(monkeypatch, stdout="не json вовсе")

    with pytest.raises(rg.QuotaCheckFailed):
        rg.fetch_core()


def test_fetch_core_raises_on_missing_core_key(monkeypatch):
    # Ответ распарсился, но неожиданной формы (нет .resources.core) — тоже
    # настоящий сбой проверки, не «квоты мало».
    _patch_gh(monkeypatch, stdout=json.dumps({"resources": {}}))

    with pytest.raises(rg.QuotaCheckFailed):
        rg.fetch_core()


# ── should_skip: порог, граница ───────────────────────────────────────────────


def test_should_skip_true_when_remaining_below_threshold():
    assert rg.should_skip({"remaining": 299}, threshold=300) is True


def test_should_skip_false_exactly_at_threshold():
    # Мутация: замена `<` на `<=` в should_skip красит именно этот тест.
    assert rg.should_skip({"remaining": 300}, threshold=300) is False


def test_should_skip_false_well_above_threshold():
    assert rg.should_skip({"remaining": 5000}, threshold=300) is False


def test_reset_human_formats_utc():
    assert rg.reset_human({"reset": 1788676450}) == "2026-09-06 06:34 UTC"


# ── main(): пути «квота ок» / «квота мала» / «настоящий сбой» ────────────────


def test_main_ok_quota_writes_skip_false(monkeypatch, tmp_path, capsys):
    _patch_gh(monkeypatch, stdout=REAL_RATE_LIMIT_RESPONSE)
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "orchestra"])

    code = rg.main()

    assert code == 0
    text = output_file.read_text(encoding="utf-8")
    assert "skip=false" in text
    assert "skip=true" not in text
    captured = capsys.readouterr()
    assert "::warning::" not in captured.out
    assert "::error::" not in captured.out


def test_main_low_quota_skips_with_warning_not_error(monkeypatch, tmp_path, capsys):
    low = json.dumps({"resources": {"core": {"limit": 1000, "used": 950, "remaining": 50, "reset": 1788676450}}})
    _patch_gh(monkeypatch, stdout=low)
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "ai-review", "--threshold", "300"])

    code = rg.main()

    # Квота мала — job обязан остаться зелёным (код 0), не упасть.
    assert code == 0
    text = output_file.read_text(encoding="utf-8")
    assert "skip=true" in text
    assert "reset=2026-09-06 06:34 UTC" in text
    captured = capsys.readouterr()
    assert "::warning::" in captured.out
    assert "::error::" not in captured.out
    assert "50/1000" in captured.out
    assert "ai-review" in captured.out


def test_main_hard_failure_is_not_confused_with_low_quota(monkeypatch, tmp_path, capsys):
    # Класс «квоты нет» и класс «запрос не прошёл по другой причине» обязаны
    # различаться и кодом возврата, и аннотацией (граница из докстринга
    # модуля). Мутация: подмена `return 1` на `return 0` в ветке
    # QuotaCheckFailed красит этот тест.
    _patch_gh(monkeypatch, returncode=1, stderr="dial tcp: connection refused")
    output_file = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "orchestra"])

    code = rg.main()

    assert code == 1
    assert not output_file.exists() or output_file.read_text(encoding="utf-8") == ""
    captured = capsys.readouterr()
    assert "::error::" in captured.out
    assert "::warning::" not in captured.out
    assert "connection refused" in captured.out


def test_default_threshold_is_300():
    # Число зафиксировано тестом, а не только прозой докстринга (обоснование
    # там же: 150-250 запросов/прогон orchestra + запас ~20%).
    assert rg.DEFAULT_THRESHOLD == 300


def test_main_without_github_output_env_does_not_raise(monkeypatch, capsys):
    # Локальный прогон/дебаг без GITHUB_OUTPUT (не в Actions) — не должен падать.
    _patch_gh(monkeypatch, stdout=REAL_RATE_LIMIT_RESPONSE)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setattr(sys, "argv", ["rate_guard.py", "--job", "test"])

    code = rg.main()

    assert code == 0
