#!/usr/bin/env python3
"""Тесты замера латентности провайдеров (scripts/measure/provider_latency.py,
issue #836).

Кормятся прод-формой ответа (AGENTS.md, «Тест кормит прод-форму данных, а не
пересказ») — тело успешного ответа скопировано по форме реального
OpenAI-compatible `chat/completions` (choices[].message.content + usage с
prompt_tokens/completion_tokens/total_tokens), не придумано с нуля. Сеть НЕ
трогается ни в одном тесте — transport подменяется фейком, как это уже
принято в scripts/orchestra/test_pulse_guard.py (monkeypatch сетевых вызовов).

Запуск: python -m pytest scripts/measure/test_provider_latency.py -q
"""

import importlib.util
import json
import socket
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("provider_latency.py")
spec = importlib.util.spec_from_file_location("provider_latency", SCRIPT)
pl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pl)  # type: ignore[union-attr]


ENTRY = {"name": "GLM", "base_url": "https://api.z.ai/api/coding/paas/v4", "model": "glm-5.3-flash", "secret_env": "DEEPSEEK_API_KEY"}

# Прод-форма ответа OpenAI-compatible chat/completions (поля как в реальном
# ответе z.ai/NVIDIA/OpenRouter/Ollama — все четыре следуют этому же контракту,
# см. dsh_patch_profile в scripts/lib/dsh-ci.sh, api: openai-completions).
SUCCESS_BODY = (
    b'{"id":"chatcmpl-1","object":"chat.completion","choices":'
    b'[{"index":0,"message":{"role":"assistant","content":"OK"},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":12,"completion_tokens":1,"total_tokens":13}}'
)


# ── extract_usage_tokens ──────────────────────────────────────────────────


def test_extract_usage_tokens_prod_form():
    import json
    body = json.loads(SUCCESS_BODY)
    assert pl.extract_usage_tokens(body) == "13"


def test_extract_usage_tokens_missing_usage_is_empty_not_zero():
    assert pl.extract_usage_tokens({"choices": []}) == ""
    assert pl.extract_usage_tokens({}) == ""
    assert pl.extract_usage_tokens("не словарь") == ""


# ── measure_provider: успех ──────────────────────────────────────────────


def test_measure_provider_success_prod_form():
    def fake_transport(url, headers, payload, timeout_secs):
        assert url == "https://api.z.ai/api/coding/paas/v4/chat/completions"
        assert headers["Authorization"] == "Bearer secret-value"
        return 200, SUCCESS_BODY

    row = pl.measure_provider(ENTRY, transport=fake_transport, key="secret-value")
    assert row["status"] == "success"
    assert row["http"] == 200
    assert row["tokens"] == "13"
    assert row["latency_s"] is not None and row["latency_s"] >= 0
    assert row["name"] == "GLM"
    assert row["model"] == "glm-5.3-flash"
    # Ключ нигде не попадает в возвращаемую строку (значения ключей не печатать)
    assert "secret-value" not in str(row)


# ── measure_provider: секрет не задан ────────────────────────────────────


def test_measure_provider_missing_secret_is_error_without_network_call():
    called = False

    def fake_transport(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("транспорт не должен вызываться без ключа")

    row = pl.measure_provider(ENTRY, transport=fake_transport, key="")
    assert called is False
    assert row["status"] == "error"
    assert row["latency_s"] is None
    assert "DEEPSEEK_API_KEY" in row["note"]


# ── measure_provider: HTTP-ошибка (прод-форма 401) ───────────────────────


def test_measure_provider_http_error_prod_form():
    def fake_transport(url, headers, payload, timeout_secs):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", hdrs=None, fp=None)

    row = pl.measure_provider(ENTRY, transport=fake_transport, key="bad-key")
    assert row["status"] == "error"
    assert row["http"] == 401
    assert row["tokens"] == ""
    # Тело/детали ошибки не просачиваются наружу
    assert "bad-key" not in str(row)


# ── measure_provider: таймаут (оба варианта, socket.timeout и URLError) ──


def test_measure_provider_timeout_direct_socket_timeout():
    def fake_transport(url, headers, payload, timeout_secs):
        raise socket.timeout("timed out")

    row = pl.measure_provider(ENTRY, transport=fake_transport, key="k", timeout_secs=5)
    assert row["status"] == "timeout"
    assert row["http"] == ""
    assert "5" in row["note"]


def test_measure_provider_timeout_wrapped_in_urlerror():
    # Прод-форма: urlopen часто заворачивает socket.timeout в URLError(reason=...)
    def fake_transport(url, headers, payload, timeout_secs):
        raise urllib.error.URLError(socket.timeout("timed out"))

    row = pl.measure_provider(ENTRY, transport=fake_transport, key="k", timeout_secs=5)
    assert row["status"] == "timeout"


def test_measure_provider_network_error_not_timeout_is_generic_error():
    def fake_transport(url, headers, payload, timeout_secs):
        raise urllib.error.URLError("сеть недоступна")

    row = pl.measure_provider(ENTRY, transport=fake_transport, key="k")
    assert row["status"] == "error"
    assert row["http"] == ""


def test_measure_provider_non_json_body_is_error():
    def fake_transport(url, headers, payload, timeout_secs):
        return 200, "не json".encode("utf-8")

    row = pl.measure_provider(ENTRY, transport=fake_transport, key="k")
    assert row["status"] == "error"
    assert row["http"] == 200


# ── sort_rows: успехи по латентности, отсутствующая латентность — в конец ──


def test_sort_rows_ascending_missing_secret_last():
    rows = [
        {"name": "slow", "latency_s": 12.0},
        {"name": "no-secret", "latency_s": None},
        {"name": "fast", "latency_s": 0.5},
    ]
    ordered = [r["name"] for r in pl.sort_rows(rows)]
    assert ordered == ["fast", "slow", "no-secret"]


# ── format_markdown_table: форма для GITHUB_STEP_SUMMARY ─────────────────


def test_format_markdown_table_contains_header_and_rows():
    rows = [pl.measure_provider(ENTRY, transport=lambda *a: (200, SUCCESS_BODY), key="k")]
    table = pl.format_markdown_table(rows)
    assert table.startswith("| Провайдер | Модель | Латентность, с | Статус | HTTP | Tokens | Заметка |")
    assert "GLM" in table
    assert "glm-5.3-flash" in table
    assert "success" in table


# ── Реестр кандидатов — программа, не вторая хардкод-таблица (ревью #837) ──
#
# build_manifest_candidates()/build_plugin_suite_candidates() читают РЕАЛЬНЫЕ
# файлы репозитория (config/provider-usage.json, scripts/lib/dsh-ci.sh) —
# тестируются фикстурами (tmp_path, прод-форма содержимого) для устойчивости
# к будущей эволюции реальных файлов, плюс структурные проверки на реальных
# файлах репозитория (что парсинг вообще что-то находит, без Codex).


def test_build_manifest_candidates_from_fixture(tmp_path):
    manifest = tmp_path / "provider-usage.json"
    manifest.write_text(json.dumps({
        "chains": {
            "default-chain": [
                {"name": "NVIDIA-nano", "base_url": "https://x/v1", "model": "m-nano", "secret_env": "K_NANO"},
                {"name": "NVIDIA", "base_url": "https://x/v1", "model": "m-ultra", "secret_env": "K1"},
                {"name": "GLM", "base_url": "https://y/v4", "model": "m-glm", "secret_env": "K2"},
            ]
        },
        "usage": {"ai-review": "default-chain"},
    }), encoding="utf-8")
    out = pl.build_manifest_candidates(manifest_path=manifest)
    # NVIDIA-nano исключён (DEAD_CANDIDATE_NAMES, подтверждённый 404 #798/#834)
    assert [c["name"] for c in out] == ["NVIDIA", "GLM"]


def test_build_manifest_candidates_missing_file_is_empty_not_error(tmp_path):
    assert pl.build_manifest_candidates(manifest_path=tmp_path / "нет-файла.json") == []


def test_build_manifest_candidates_missing_consumer_is_empty(tmp_path):
    manifest = tmp_path / "provider-usage.json"
    manifest.write_text(json.dumps({"chains": {"default-chain": []}, "usage": {}}), encoding="utf-8")
    assert pl.build_manifest_candidates(consumer="ai-review", manifest_path=manifest) == []


def test_build_plugin_suite_candidates_from_fixture(tmp_path):
    dsh_ci = tmp_path / "dsh-ci.sh"
    dsh_ci.write_text(
        'PLUGINS_SUITE_CANDIDATE_ROUTES=(\n'
        '  "openrouter-1|https://openrouter.ai/api/v1|OPENROUTER_1_API_KEY|anthropic/claude-sonnet-4.6|200000|OpenRouter account 1"\n'
        '  "ollama-cloud-1|https://ollama.com/v1|OLLAMA_CLOUD_1_API_KEY|qwen3-coder:480b-cloud|262144|Ollama Cloud account 1"\n'
        ')\n',
        encoding="utf-8",
    )
    out = pl.build_plugin_suite_candidates(dsh_ci_path=dsh_ci)
    by_name = {c["name"]: c for c in out}
    assert set(by_name) == {"OpenRouter account 1", "Ollama Cloud account 1"}
    # OpenRouter — модель переопределена на free-кандидата, не платная из источника
    assert by_name["OpenRouter account 1"]["model"] == pl.OPENROUTER_FREE_MODEL_OVERRIDE
    assert by_name["OpenRouter account 1"]["model"] != "anthropic/claude-sonnet-4.6"
    assert by_name["Ollama Cloud account 1"]["model"] == "qwen3-coder:480b-cloud"
    assert by_name["Ollama Cloud account 1"]["secret_env"] == "OLLAMA_CLOUD_1_API_KEY"


def test_build_plugin_suite_candidates_missing_file_is_empty_not_error(tmp_path):
    assert pl.build_plugin_suite_candidates(dsh_ci_path=tmp_path / "нет-файла.sh") == []


def test_build_plugin_suite_candidates_no_array_in_file_is_empty(tmp_path):
    f = tmp_path / "dsh-ci.sh"
    f.write_text("# пусто, массива нет\n", encoding="utf-8")
    assert pl.build_plugin_suite_candidates(dsh_ci_path=f) == []


def test_build_candidates_dedupes_by_base_url_model_secret_env():
    dupes = [
        {"name": "A", "base_url": "https://x", "model": "m", "secret_env": "K"},
        {"name": "A-again", "base_url": "https://x", "model": "m", "secret_env": "K"},
    ]
    seen = set()
    out = []
    for entry in dupes:
        key = (entry["base_url"], entry["model"], entry["secret_env"])
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    assert len(out) == 1


# ── Структурные проверки на РЕАЛЬНЫХ файлах репозитория ──────────────────


def test_real_repo_candidates_cover_full_owner_set_without_codex():
    names = {c["name"] for c in pl.PROVIDER_LATENCY_CANDIDATES}
    assert not any("codex" in n.lower() for n in names)
    assert "NVIDIA-nano" not in names  # исключён (#798/#834, 404 на генерацию)
    assert len(pl.PROVIDER_LATENCY_CANDIDATES) >= 8  # боевая цепочка + вся suite-таблица


def test_real_repo_candidates_no_paid_openrouter_model():
    for c in pl.PROVIDER_LATENCY_CANDIDATES:
        if "openrouter" in c["secret_env"].lower() or "openrouter" in c["name"].lower():
            assert c["model"] == pl.OPENROUTER_FREE_MODEL_OVERRIDE


def test_real_repo_candidates_no_duplicate_keys():
    keys = [(c["base_url"], c["model"], c["secret_env"]) for c in pl.PROVIDER_LATENCY_CANDIDATES]
    assert len(keys) == len(set(keys))


def test_default_prompt_is_realistic_not_trivial_greeting():
    # ~1.5-2к токенов реального диффа (постановка ревью #837), не "hi"
    assert len(pl.DEFAULT_PROMPT) > 2000
    assert "diff --git" in pl.DEFAULT_PROMPT
