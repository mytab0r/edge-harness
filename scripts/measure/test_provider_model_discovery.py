#!/usr/bin/env python3
"""Тесты discovery живых model id (scripts/measure/provider_model_discovery.py,
issue #848). Сеть НЕ трогается ни в одном тесте — листинг подменяется фейковым
`transport`, верификация — фейковой `measure` (та же точка подмены, что уже
использует provider_latency.measure_provider через свой параметр `transport`).

Прод-форма ответов скопирована по форме реальных API (OpenAI-style
`/v1/models`, нативный Ollama `/api/tags`, OpenRouter `/v1/models` с
`pricing`/`context_length`) — AGENTS.md, «Тест кормит прод-форму данных».

Запуск: python -m pytest scripts/measure/test_provider_model_discovery.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json

SCRIPT = Path(__file__).with_name("provider_model_discovery.py")
spec = importlib.util.spec_from_file_location("provider_model_discovery", SCRIPT)
pmd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pmd)  # type: ignore[union-attr]


# ── family_of / build_list_url ────────────────────────────────────────────


def test_family_of_strips_trailing_account_number():
    assert pmd.family_of("nvidia-nim-1") == "nvidia-nim"
    assert pmd.family_of("nvidia-nim-2") == "nvidia-nim"
    assert pmd.family_of("ollama-cloud-3") == "ollama-cloud"
    assert pmd.family_of("openrouter-1") == "openrouter"


def test_family_of_no_trailing_number_returns_as_is():
    assert pmd.family_of("zai-1") == "zai"  # тоже число, family == "zai"
    assert pmd.family_of("no-number") == "no-number"


def test_build_list_url_openai_models():
    assert pmd.build_list_url("https://integrate.api.nvidia.com/v1", "openai_models") \
        == "https://integrate.api.nvidia.com/v1/models"


def test_build_list_url_openrouter_free_same_as_openai():
    assert pmd.build_list_url("https://openrouter.ai/api/v1", "openrouter_free") \
        == "https://openrouter.ai/api/v1/models"


def test_build_list_url_ollama_tags_strips_v1_suffix():
    assert pmd.build_list_url("https://ollama.com/v1", "ollama_tags") == "https://ollama.com/api/tags"


def test_build_list_url_unknown_strategy_raises():
    import pytest
    with pytest.raises(ValueError):
        pmd.build_list_url("https://x/v1", "no-such-strategy")


# ── extract_* — прод-форма ответов ────────────────────────────────────────


def test_extract_openai_style_ids_prod_form():
    parsed = {"object": "list", "data": [
        {"id": "deepseek-ai/deepseek-v3.1", "object": "model"},
        {"id": "nvidia/llama-3.3-nemotron-super-49b-v1", "object": "model"},
    ]}
    assert pmd.extract_openai_style_ids(parsed) == [
        "deepseek-ai/deepseek-v3.1", "nvidia/llama-3.3-nemotron-super-49b-v1"]


def test_extract_openai_style_ids_missing_data_is_empty():
    assert pmd.extract_openai_style_ids({}) == []
    assert pmd.extract_openai_style_ids({"data": "не список"}) == []


def test_extract_ollama_tags_ids_prod_form():
    parsed = {"models": [{"name": "qwen3-coder:480b-cloud", "size": 1}, {"name": "gpt-oss:120b-cloud"}]}
    assert pmd.extract_ollama_tags_ids(parsed) == ["qwen3-coder:480b-cloud", "gpt-oss:120b-cloud"]


def test_extract_openrouter_free_filters_only_zero_pricing():
    parsed = {"data": [
        {"id": "free/model-a", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 128000},
        {"id": "paid/model-b", "pricing": {"prompt": "0.000002", "completion": "0.000006"}, "context_length": 200000},
        {"id": "free/model-c", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 32000},
    ]}
    ids, context_by_id = pmd.extract_openrouter_free(parsed)
    assert ids == ["free/model-a", "free/model-c"]
    assert context_by_id == {"free/model-a": 128000, "free/model-c": 32000}


def test_extract_openrouter_free_missing_pricing_is_skipped_not_crash():
    parsed = {"data": [{"id": "no-pricing/model"}]}
    ids, context_by_id = pmd.extract_openrouter_free(parsed)
    assert ids == []
    assert context_by_id == {}


# ── rank_all / pick_best ──────────────────────────────────────────────────


def test_rank_all_prioritizes_keyword_order():
    ids = ["random/model", "vendor/qwen3-coder-480b", "vendor/deepseek-r1-distill"]
    ranked = pmd.rank_all(ids)
    # deepseek-r1 стоит раньше qwen3-coder в CODING_RANK_KEYWORDS
    assert ranked[0] == "vendor/deepseek-r1-distill"
    assert ranked[1] == "vendor/qwen3-coder-480b"
    assert ranked[2] == "random/model"


def test_rank_all_tiebreak_by_context_length_within_same_keyword():
    ids = ["a/deepseek-r1-small", "b/deepseek-r1-large"]
    context_by_id = {"a/deepseek-r1-small": 32000, "b/deepseek-r1-large": 128000}
    ranked = pmd.rank_all(ids, context_by_id)
    assert ranked[0] == "b/deepseek-r1-large"


def test_pick_best_returns_none_on_empty_listing():
    model, note = pmd.pick_best([])
    assert model is None
    assert "пуст" in note


def test_pick_best_falls_back_to_largest_context_when_no_keyword_matches():
    ids = ["vendor/unknown-a", "vendor/unknown-b"]
    context_by_id = {"vendor/unknown-a": 8000, "vendor/unknown-b": 64000}
    model, note = pmd.pick_best(ids, context_by_id)
    assert model == "vendor/unknown-b"
    assert "контекстным окном" in note


def test_pick_best_names_matched_keyword():
    model, note = pmd.pick_best(["vendor/qwen3-coder-480b"])
    assert model == "vendor/qwen3-coder-480b"
    assert "qwen3-coder" in note


# ── discover_listing: перебор стратегий (Ollama Cloud) ─────────────────────


def test_discover_listing_falls_back_to_second_strategy_on_first_failure():
    calls = []

    def fake_transport(url, headers, timeout_secs):
        calls.append(url)
        if url.endswith("/v1/models"):
            raise __import__("urllib.error", fromlist=["error"]).HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
        assert url.endswith("/api/tags")
        return 200, json.dumps({"models": [{"name": "qwen3-coder:480b-cloud"}]}).encode()

    route = {"alias": "ollama-cloud-1", "base_url": "https://ollama.com/v1",
              "secret_env": "OLLAMA_CLOUD_1_API_KEY", "family": "ollama-cloud",
              "display_name": "Ollama Cloud account 1"}
    result = pmd.discover_listing(route, "key", transport=fake_transport)
    assert result["strategy"] == "ollama_tags"
    assert result["ids"] == ["qwen3-coder:480b-cloud"]
    assert len(calls) == 2


def test_discover_listing_no_strategy_matches_family_is_empty():
    result = pmd.discover_listing({"family": "unknown-family", "base_url": "https://x/v1"}, "key")
    assert result["ids"] == []
    assert "нет стратегий" in result["note"]


# ── verify_candidates: несколько попыток, первый успех выигрывает ─────────


def test_verify_candidates_stops_at_first_success():
    calls = []

    def fake_measure(entry, prompt=None, timeout_secs=None, key=None):
        calls.append(entry["model"])
        if entry["model"] == "candidate-1":
            return {"status": "error", "http": 429, "note": "rate limited"}
        return {"status": "success", "http": 200, "note": ""}

    route = {"display_name": "OpenRouter account 1", "base_url": "https://openrouter.ai/api/v1",
              "secret_env": "OPENROUTER_1_API_KEY"}
    result = pmd.verify_candidates(route, ["candidate-1", "candidate-2", "candidate-3"], "key",
                                     measure=fake_measure)
    assert result["verified"] is True
    assert result["verified_model"] == "candidate-2"
    assert calls == ["candidate-1", "candidate-2"]  # не тронул candidate-3 после успеха


def test_verify_candidates_exhausts_all_attempts_when_none_succeed():
    def fake_measure(entry, prompt=None, timeout_secs=None, key=None):
        return {"status": "error", "http": 404, "note": "not found"}

    route = {"display_name": "X", "base_url": "https://x/v1", "secret_env": "K"}
    result = pmd.verify_candidates(route, ["c1", "c2"], "key", measure=fake_measure)
    assert result["verified"] is False
    assert result["verified_model"] is None
    assert len(result["attempts"]) == 2


def test_verify_candidates_respects_max_attempts_cap():
    calls = []

    def fake_measure(entry, prompt=None, timeout_secs=None, key=None):
        calls.append(entry["model"])
        return {"status": "error", "http": 404, "note": ""}

    route = {"display_name": "X", "base_url": "https://x/v1", "secret_env": "K"}
    pmd.verify_candidates(route, ["c1", "c2", "c3", "c4"], "key", max_attempts=2, measure=fake_measure)
    assert calls == ["c1", "c2"]


# ── discover_route: полный цикл, секрет не задан ────────────────────────


def test_discover_route_missing_secret_skips_network_entirely():
    called = {"listing": False, "verify": False}

    def fake_transport(*a, **kw):
        called["listing"] = True
        raise AssertionError("листинг не должен вызываться без секрета")

    def fake_measure(*a, **kw):
        called["verify"] = True
        raise AssertionError("верификация не должна вызываться без секрета")

    route = {"alias": "nvidia-nim-1", "base_url": "https://integrate.api.nvidia.com/v1",
              "secret_env": "NVIDIA_NIM_1_API_KEY", "family": "nvidia-nim",
              "display_name": "NVIDIA NIM account 1"}
    result = pmd.discover_route(route, transport=fake_transport, measure=fake_measure)
    assert called == {"listing": False, "verify": False}
    assert result["secret_present"] is False
    assert result["recommended"] is None
    assert "NVIDIA_NIM_1_API_KEY" in result["note"]


def test_discover_route_openrouter_lists_public_without_secret():
    def fake_transport(url, headers, timeout_secs):
        assert "Authorization" not in headers
        return 200, json.dumps({"data": [
            {"id": "vendor/deepseek-r1-free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 64000},
        ]}).encode()

    route = {"alias": "openrouter-1", "base_url": "https://openrouter.ai/api/v1",
              "secret_env": "OPENROUTER_1_API_KEY", "family": "openrouter",
              "display_name": "OpenRouter account 1"}
    result = pmd.discover_route(route, transport=fake_transport)
    assert result["secret_present"] is False
    assert result["recommended"] == "vendor/deepseek-r1-free"
    assert result["verified"] is False
    assert "верификация пропущена" in result["note"]


def test_discover_route_full_success_with_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_NIM_1_API_KEY", "secret-value")

    def fake_transport(url, headers, timeout_secs):
        assert headers["Authorization"] == "Bearer secret-value"
        return 200, json.dumps({"data": [{"id": "deepseek-ai/deepseek-r1"}]}).encode()

    def fake_measure(entry, prompt=None, timeout_secs=None, key=None):
        assert key == "secret-value"
        assert entry["model"] == "deepseek-ai/deepseek-r1"
        return {"status": "success", "http": 200, "note": ""}

    route = {"alias": "nvidia-nim-1", "base_url": "https://integrate.api.nvidia.com/v1",
              "secret_env": "NVIDIA_NIM_1_API_KEY", "family": "nvidia-nim",
              "display_name": "NVIDIA NIM account 1"}
    result = pmd.discover_route(route, transport=fake_transport, measure=fake_measure)
    assert result["recommended"] == "deepseek-ai/deepseek-r1"
    assert result["verified"] is True
    assert result["verified_model"] == "deepseek-ai/deepseek-r1"
    # Значение ключа нигде не просачивается в результат
    assert "secret-value" not in str(result)


# ── load_routes_by_family / format_markdown_report — на реальном файле ────


def test_load_routes_by_family_covers_expected_families():
    grouped = pmd.load_routes_by_family()
    assert set(grouped) == {"nvidia-nim", "ollama-cloud", "openrouter"}
    assert len(grouped["nvidia-nim"]) == 2
    assert len(grouped["ollama-cloud"]) == 3
    assert len(grouped["openrouter"]) == 2
    for routes in grouped.values():
        for route in routes:
            assert {"alias", "base_url", "secret_env", "family", "display_name"} <= route.keys()


def test_load_routes_by_family_missing_file_is_empty(tmp_path):
    assert pmd.load_routes_by_family(dsh_ci_path=tmp_path / "нет-файла.sh") == {}


def test_format_markdown_report_contains_header_and_rows():
    grouped_results = {"nvidia-nim": [{
        "alias": "nvidia-nim-1", "display_name": "NVIDIA NIM account 1", "family": "nvidia-nim",
        "secret_present": True, "strategy": "openai_models", "listing_count": 3,
        "recommended": "deepseek-ai/deepseek-r1", "verified_model": "deepseek-ai/deepseek-r1",
        "verified": True, "attempts": [{"candidate": "deepseek-ai/deepseek-r1", "status": "success",
                                          "http": 200, "note": ""}], "note": "",
    }]}
    table = pmd.format_markdown_report(grouped_results)
    assert table.startswith("| Семья | Аккаунт |")
    assert "nvidia-nim" in table
    assert "deepseek-ai/deepseek-r1" in table
    assert "да" in table


# ── main(): fail loud при пустом наборе семейств ──────────────────────────


def test_main_fails_loud_when_no_family_routes_found(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pmd, "DSH_CI_SH", tmp_path / "нет-файла.sh")
    rc = pmd.main()
    err = capsys.readouterr().err
    assert rc == 1
    assert "discovery не нашёл" in err
