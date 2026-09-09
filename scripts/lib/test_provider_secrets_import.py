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
        return "жива"

    monkeypatch.setattr(psi, "set_secret", _forbidden)
    monkeypatch.setattr(psi, "set_variable", _forbidden)
    monkeypatch.setattr(psi, "probe_provider", _fake_probe)

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
    monkeypatch.setattr(psi, "probe_provider", _forbidden)

    rc = psi.main([
        "--export-file", str(export_path),
        "--dsh-ci-path", str(dsh_ci_path),
        "--no-probe",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert FIXTURE_MARKER not in captured.out
    assert FIXTURE_MARKER not in captured.err


def test_probe_provider_never_leaks_key_on_http_error(monkeypatch):
    """Задача #777, критерий 6: даже если провайдер вернёт ошибку, чьё тело
    содержит подстроку ключа (гипотетическое эхо), probe_provider не читает
    msg/тело — читает только error.code. Мутация: если когда-нибудь код
    начнёт возвращать str(error) целиком, этот тест покраснеет."""
    import urllib.error

    def _raise_http_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://example.test/models", 401,
            f"unauthorized: your key {FIXTURE_MARKER} is invalid", {}, None,
        )

    monkeypatch.setattr(psi.urllib.request, "urlopen", _raise_http_error)
    status = psi.probe_provider("https://example.test", FIXTURE_MARKER)
    assert status == "ключ неверен"
    assert FIXTURE_MARKER not in status


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
        stale_but_alive.id: "жива",
        confirmed_dead.id: "ключ неверен",
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
    probe_results = {quota_acc.id: "квота исчерпана", dead_acc.id: "ключ неверен"}
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
    probe_results = {acc.id: "неизвестно (сеть: [Errno -2] Name or service not known)"}
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
    probe_results = {glm_acc.id: "ключ неверен"}
    selection = psi.select_accounts([glm_acc], routes, probe_results=probe_results)
    by_secret = {a.route.secret_env: a for a in selection.assignments}
    assert by_secret["ZAI_1_API_KEY"].account is None
    assert by_secret["ZAI_1_API_KEY"].empty_reason is not None
    assert "1 кандидат" in by_secret["ZAI_1_API_KEY"].empty_reason
    assert "ключ неверен" in by_secret["ZAI_1_API_KEY"].empty_reason
    assert glm_acc in selection.probe_excluded
    assert len(selection.overflow) == 0


def test_render_report_shows_export_date_and_rank_source(monkeypatch, export_path, dsh_ci_path):
    data = psi.load_export(str(export_path))
    routes = psi.parse_suite_routes(dsh_ci_path)
    accounts = psi.accounts_from_export(data)
    selection = psi.select_accounts(accounts, routes)
    report = psi.render_report(selection, {}, "не проверялась", False, "2026-08-25 (дата из имени файла)")
    assert "2026-08-25" in report
    assert "ПРЕДОХРАНИТЕЛЯ" in report
    assert "источник: снимок" in report


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
    # Задача #786: --body-file непортируем между версиями gh (2.85.0 не знает
    # флаг у gh secret/variable set) — значение подаётся ТОЛЬКО через input=,
    # argv не несёт ни значения, ни этого флага.
    assert "--body-file" not in args


# ── Гвардия класса «непортируемый флаг gh» (задача #786) ─────────────────
#
# gh secret set / gh variable set читают значение из stdin, когда --body/
# --body-file не передан вовсе — добавлять --body-file было лишним и на
# установленной у владельца версии gh (2.85.0) роняло вызов целиком
# (`unknown flag: --body-file`), из-за чего НИ ОДИН секрет не записывался,
# хотя код возврата ловился и печатался. Проверяем по ИСХОДНИКУ, что литерал
# не вернётся тихо при будущей правке (по образцу test_pagination_guard.py).
#
# Мутация, которой доказана гвардия: верни в set_secret/set_variable
# `"--body-file", "-"` в списке argv — этот тест краснеет; убери — снова
# зелёный (проверено вручную при внедрении гвардии).


def test_set_secret_and_set_variable_source_never_contains_body_file_flag():
    """По AST, не по подстроке текста функции: докстринг обеих функций сам
    объясняет, ПОЧЕМУ --body-file не добавлен (задача #786), и упоминает этот
    литерал прозой — подстрочная проверка текста функции ловила бы и это
    объяснение как нарушение. Признак настоящего нарушения — строковый
    литерал `--body-file` в КОДЕ функции (argv списка gh), не в докстринге."""
    import ast

    source = Path(psi.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("set_secret", "set_variable"):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]  # пропускаем докстринг — там литерал упомянут прозой, не кодом
            for sub in body:
                for inner in ast.walk(sub):
                    if isinstance(inner, ast.Constant) and inner.value == "--body-file":
                        offenders.append(node.name)
    assert offenders == [], (
        "непортируемый флаг gh (--body-file, задача #786, gh 2.85.0: "
        f"'unknown flag') снова в коде функции: {offenders}. gh secret/variable "
        "set читает значение из stdin без флага вовсе — не добавляй "
        "--body-file обратно."
    )


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
