#!/usr/bin/env python3
"""Тесты персистентного состояния квоты провайдеров (#857,
openspec/changes/provider-quota-gating): scripts/orchestra/provider_quota_state.py.

Чистые функции (parse/merge/expire) тестируются без сети. load_quota_state/
save_quota_state — на фейковом gh_func (тот же приём, что FakeGh в
test_scheduler.py: маршрутизация по подстроке аргументов).

Запуск: python -m pytest scripts/orchestra/test_provider_quota_state.py -q
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "provider_quota_state", _DIR / "provider_quota_state.py")
pqs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pqs)  # type: ignore[union-attr]


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ── parse_reset_hint_pairs ────────────────────────────────────────────────


def test_parse_reset_hint_pairs_prod_form_both_date_shapes():
    # Прод-форма #727: «Your limit will reset at 2026-09-10 08:51:55»
    # (живой прогон 34176910458) и ISO-форма смоук-фикстуры — обе формы.
    pairs = pqs.parse_reset_hint_pairs("GLM: 2026-09-10 08:51:55; NVIDIA: 2026-09-11T00:00:00Z")
    assert pairs["GLM"] == utc(2026, 9, 10, 8, 51, 55)
    assert pairs["NVIDIA"] == utc(2026, 9, 11, 0, 0, 0)


def test_parse_reset_hint_pairs_empty_or_garbage_is_empty_dict():
    assert pqs.parse_reset_hint_pairs("") == {}
    assert pqs.parse_reset_hint_pairs("не дата вовсе") == {}


# ── merge_reset_hints ─────────────────────────────────────────────────────


def test_merge_reset_hints_adds_future_date():
    state, changed = pqs.merge_reset_hints({}, "GLM: 2026-09-10 08:51:55", utc(2026, 9, 8, 0, 0, 0))
    assert changed is True
    assert state == {"GLM": "2026-09-10T08:51:55Z"}


def test_merge_reset_hints_skips_past_date():
    # Прошедшая дата не несёт пользы гейту — не засоряем состояние.
    state, changed = pqs.merge_reset_hints({}, "GLM: 2026-09-01 00:00:00", utc(2026, 9, 8, 0, 0, 0))
    assert changed is False
    assert state == {}


def test_merge_reset_hints_idempotent_same_date():
    existing = {"GLM": "2026-09-10T08:51:55Z"}
    state, changed = pqs.merge_reset_hints(existing, "GLM: 2026-09-10 08:51:55", utc(2026, 9, 8, 0, 0, 0))
    assert changed is False
    assert state == existing


def test_merge_reset_hints_updates_changed_date():
    existing = {"GLM": "2026-09-10T08:51:55Z"}
    state, changed = pqs.merge_reset_hints(existing, "GLM: 2026-09-12 00:00:00", utc(2026, 9, 8, 0, 0, 0))
    assert changed is True
    assert state == {"GLM": "2026-09-12T00:00:00Z"}


def test_merge_reset_hints_preserves_other_providers():
    existing = {"NVIDIA": "2026-09-20T00:00:00Z"}
    state, changed = pqs.merge_reset_hints(existing, "GLM: 2026-09-10 08:51:55", utc(2026, 9, 8, 0, 0, 0))
    assert changed is True
    assert state == {"NVIDIA": "2026-09-20T00:00:00Z", "GLM": "2026-09-10T08:51:55Z"}


# ── expire_stale ──────────────────────────────────────────────────────────


def test_expire_stale_drops_past_dates():
    state = {"GLM": "2026-09-10T08:51:55Z", "NVIDIA": "2026-09-20T00:00:00Z"}
    new_state, expired = pqs.expire_stale(state, utc(2026, 9, 15, 0, 0, 0))
    assert new_state == {"NVIDIA": "2026-09-20T00:00:00Z"}
    assert expired == ["GLM"]


def test_expire_stale_drops_unparseable_garbage():
    state = {"GLM": "не дата вовсе"}
    new_state, expired = pqs.expire_stale(state, utc(2026, 9, 15, 0, 0, 0))
    assert new_state == {}
    assert expired == ["GLM"]


def test_expire_stale_noop_when_nothing_expired():
    state = {"NVIDIA": "2026-09-20T00:00:00Z"}
    new_state, expired = pqs.expire_stale(state, utc(2026, 9, 15, 0, 0, 0))
    assert new_state == state
    assert expired == []


# ── load_quota_state / save_quota_state (фейковый gh_func) ──────────────


REPO = "mytab0r/edge-harness"


def test_load_quota_state_variable_not_set_returns_empty():
    def fake_gh(*args):
        raise RuntimeError("gh api ...: HTTP 404: Not Found (https://api.github.com/...)")
    assert pqs.load_quota_state(REPO, fake_gh) == {}


def test_load_quota_state_parses_value_json():
    def fake_gh(*args):
        return {"name": pqs.QUOTA_VAR_NAME, "value": '{"GLM": "2026-09-10T08:51:55Z"}'}
    assert pqs.load_quota_state(REPO, fake_gh) == {"GLM": "2026-09-10T08:51:55Z"}


def test_load_quota_state_empty_value_is_empty_dict():
    def fake_gh(*args):
        return {"name": pqs.QUOTA_VAR_NAME, "value": ""}
    assert pqs.load_quota_state(REPO, fake_gh) == {}


def test_load_quota_state_invalid_json_raises_loud():
    def fake_gh(*args):
        return {"name": pqs.QUOTA_VAR_NAME, "value": "не json"}
    with pytest.raises(RuntimeError, match="невалидный JSON"):
        pqs.load_quota_state(REPO, fake_gh)


def test_load_quota_state_non_object_json_raises_loud():
    def fake_gh(*args):
        return {"name": pqs.QUOTA_VAR_NAME, "value": "[1, 2, 3]"}
    with pytest.raises(RuntimeError, match="JSON-объект"):
        pqs.load_quota_state(REPO, fake_gh)


def test_load_quota_state_propagates_non_404_error():
    def fake_gh(*args):
        raise RuntimeError("gh api ...: HTTP 500: Internal Server Error")
    with pytest.raises(RuntimeError, match="500"):
        pqs.load_quota_state(REPO, fake_gh)


def test_save_quota_state_patches_existing_variable():
    calls = []

    def fake_gh(*args):
        calls.append(args)
        return None

    pqs.save_quota_state(REPO, {"GLM": "2026-09-10T08:51:55Z"}, fake_gh)
    assert len(calls) == 1
    assert calls[0][0] == "-X" and calls[0][1] == "PATCH"
    assert f"repos/{REPO}/actions/variables/{pqs.QUOTA_VAR_NAME}" in calls[0]
    assert any("value={\"GLM\": \"2026-09-10T08:51:55Z\"}" in arg for arg in calls[0])


def test_save_quota_state_falls_back_to_post_when_variable_missing():
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[1] == "PATCH":
            raise RuntimeError("gh api ...: HTTP 404: Not Found")
        return None

    pqs.save_quota_state(REPO, {"GLM": "2026-09-10T08:51:55Z"}, fake_gh)
    assert len(calls) == 2
    assert calls[0][1] == "PATCH"
    assert calls[1][1] == "POST"
    assert f"repos/{REPO}/actions/variables" in calls[1]


def test_save_quota_state_propagates_non_404_patch_error():
    def fake_gh(*args):
        raise RuntimeError("gh api ...: HTTP 500: Internal Server Error")
    with pytest.raises(RuntimeError, match="500"):
        pqs.save_quota_state(REPO, {"GLM": "2026-09-10T08:51:55Z"}, fake_gh)
