#!/usr/bin/env python3
"""Тесты scripts/lib/merge_reactions.py (issue #955, следствие #929).

Прод-форма: `has_run_for_sha` фикстуры — реальная форма ответа GitHub Actions
API `GET .../workflows/{workflow}/runs?head_sha=...` (поле `workflow_runs`,
список объектов с `head_sha`); `dispatch_workflow`/`react_to_merge` кормятся
теми же аргументами (`-X`, `POST`, путь, `-f ref=...`), какими сценарий
диспатча уже вызывает `gh()` в scheduler.py (worker.yml dispatch, тот же
контракт).

Запуск: python -m pytest scripts/lib/test_merge_reactions.py -q
"""

import importlib.util
import json
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "merge_reactions.py"
spec = importlib.util.spec_from_file_location("merge_reactions", SCRIPT)
mr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mr)  # type: ignore[union-attr]


REGISTRY = [
    {"prefix": "", "workflow": "repo-ci.yml"},
    {"prefix": "", "workflow": "codeql.yml"},
    {"prefix": "cf-worker/", "workflow": "worker-ci.yml"},
    {"prefix": "cf-worker/", "workflow": "deploy-worker.yml"},
    {"prefix": "dsh-edge/", "workflow": "deploy-dsh-edge.yml"},
]


def files(*names):
    return [{"filename": n} for n in names]


class FakeGh:
    """Маршрутизатор по подстроке пути, тот же приём, что уже используют
    test_scheduler.py/test_repo_invariants.py. Фиксирует вызовы для
    проверки порядка/содержимого дедупа."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")


# ══════════════════════════════════════════════════════════════════════════
# load_registry — контракт файла реестра
# ══════════════════════════════════════════════════════════════════════════


def test_load_registry_reads_real_config_file():
    # Реальный файл репозитория — не фикстура: если кто-то сломает
    # config/merge-reactions.json, этот тест первым укажет на файл, а не на
    # тестовую копию.
    registry = mr.load_registry()
    workflows = {entry["workflow"] for entry in registry}
    assert {"repo-ci.yml", "codeql.yml", "worker-ci.yml",
            "deploy-worker.yml", "deploy-dsh-edge.yml"} <= workflows
    assert all("prefix" in entry and entry.get("workflow") for entry in registry)


def test_load_registry_missing_file_raises_registry_error(tmp_path):
    with pytest.raises(mr.RegistryError, match="не найден"):
        mr.load_registry(tmp_path / "nonexistent.json")


def test_load_registry_invalid_json_raises_registry_error(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{не json", encoding="utf-8")
    with pytest.raises(mr.RegistryError, match="невалидный JSON"):
        mr.load_registry(path)


def test_load_registry_empty_reactions_raises_registry_error(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"reactions": []}), encoding="utf-8")
    with pytest.raises(mr.RegistryError, match="пусто"):
        mr.load_registry(path)


def test_load_registry_entry_without_workflow_raises_registry_error(tmp_path):
    path = tmp_path / "bad-entry.json"
    path.write_text(json.dumps({"reactions": [{"prefix": "cf-worker/"}]}), encoding="utf-8")
    with pytest.raises(mr.RegistryError, match="prefix.*workflow"):
        mr.load_registry(path)


# ══════════════════════════════════════════════════════════════════════════
# matching_workflows — чистая функция
# ══════════════════════════════════════════════════════════════════════════


def test_matching_workflows_empty_prefix_matches_any_file():
    assert mr.matching_workflows(files("scripts/orchestra/scheduler.py"), REGISTRY) == \
        ["repo-ci.yml", "codeql.yml"]


def test_matching_workflows_prefixed_entry_matches_only_its_path():
    result = mr.matching_workflows(files("cf-worker/src/harness.ts"), REGISTRY)
    assert result == ["repo-ci.yml", "codeql.yml", "worker-ci.yml", "deploy-worker.yml"]


def test_matching_workflows_dsh_edge_path_does_not_trigger_cf_worker_entries():
    result = mr.matching_workflows(files("dsh-edge/upstream.json"), REGISTRY)
    assert "worker-ci.yml" not in result
    assert "deploy-worker.yml" not in result
    assert "deploy-dsh-edge.yml" in result


def test_matching_workflows_dedups_same_workflow_across_entries():
    registry = [
        {"prefix": "cf-worker/", "workflow": "deploy-worker.yml"},
        {"prefix": ".github/workflows/deploy-worker.yml", "workflow": "deploy-worker.yml"},
    ]
    result = mr.matching_workflows(files("cf-worker/src/config.ts"), registry)
    assert result == ["deploy-worker.yml"]


# ══════════════════════════════════════════════════════════════════════════
# has_run_for_sha — прод-форма Actions API
# ══════════════════════════════════════════════════════════════════════════


def test_has_run_for_sha_true_when_workflow_runs_nonempty():
    fake = FakeGh({
        "workflows/deploy-worker.yml/runs?head_sha=dbe8c9956d": {
            "total_count": 1,
            "workflow_runs": [{"id": 1, "head_sha": "dbe8c9956d", "status": "completed"}],
        },
    })
    assert mr.has_run_for_sha(fake, "o/r", "deploy-worker.yml", "dbe8c9956d") is True


def test_has_run_for_sha_false_when_no_runs():
    fake = FakeGh({
        "workflows/deploy-worker.yml/runs?head_sha=deadbeef": {"total_count": 0, "workflow_runs": []},
    })
    assert mr.has_run_for_sha(fake, "o/r", "deploy-worker.yml", "deadbeef") is False


# ══════════════════════════════════════════════════════════════════════════
# react_to_merge — единая функция диспатча + дедуп (класс #929)
# ══════════════════════════════════════════════════════════════════════════


def test_react_to_merge_requires_head_sha():
    fake = FakeGh({})
    with pytest.raises(RuntimeError, match="head_sha"):
        mr.react_to_merge(fake, "o/r", files("cf-worker/x"), "", registry=REGISTRY)


def test_react_to_merge_dispatches_matched_workflows_when_no_existing_run():
    fake = FakeGh({
        "runs?head_sha=abc123&per_page=1": {"workflow_runs": []},
        "-X POST": None,
    })
    actions = mr.react_to_merge(fake, "o/r", files("cf-worker/src/config.ts"), "abc123", registry=REGISTRY)
    dispatched = [w for w in ("repo-ci.yml", "codeql.yml", "worker-ci.yml", "deploy-worker.yml")]
    assert len(actions) == len(dispatched)
    assert all("🚀" in a for a in actions)
    posts = [c for c in fake.calls if c.startswith("-X POST")]
    assert len(posts) == len(dispatched)
    for workflow in dispatched:
        assert any(f"workflows/{workflow}/dispatches" in c for c in posts)


def test_react_to_merge_skips_workflow_with_existing_run_on_same_sha():
    # Живой класс #929: deploy-worker.yml получил ДВА прогона на один и тот
    # же headSha (push 16:12:46Z + dispatch 16:12:50Z). Этот тест — прямое
    # доказательство того, что дедуп закрывает именно этот случай:
    # снятие проверки has_run_for_sha внутри react_to_merge красит этот тест
    # (дубль-POST появится там, где его быть не должно).
    fake = FakeGh({
        "workflows/deploy-worker.yml/runs?head_sha=dbe8c9956d": {
            "workflow_runs": [{"id": 1, "head_sha": "dbe8c9956d"}],
        },
        "workflows/worker-ci.yml/runs?head_sha=dbe8c9956d": {"workflow_runs": []},
        "workflows/repo-ci.yml/runs?head_sha=dbe8c9956d": {"workflow_runs": []},
        "workflows/codeql.yml/runs?head_sha=dbe8c9956d": {"workflow_runs": []},
        "-X POST": None,
    })
    actions = mr.react_to_merge(
        fake, "o/r", files("cf-worker/src/config.ts"), "dbe8c9956d", registry=REGISTRY)
    assert any("⏭️" in a and "deploy-worker.yml" in a for a in actions)
    posts = [c for c in fake.calls if c.startswith("-X POST")]
    assert not any("deploy-worker.yml/dispatches" in c for c in posts)
    assert any("worker-ci.yml/dispatches" in c for c in posts)


def test_react_to_merge_no_matched_workflow_makes_no_network_call():
    fake = FakeGh({})
    actions = mr.react_to_merge(
        fake, "o/r", files("docs/README.md"), "sha1",
        registry=[{"prefix": "cf-worker/", "workflow": "deploy-worker.yml"}])
    assert actions == []
    assert fake.calls == []


def test_react_to_merge_one_workflow_failure_does_not_abort_the_rest():
    # Найдено ревью PR #956: раньше вся функция обрывалась ПЕРВЫМ же
    # RuntimeError (has_run_for_sha), и caller (after_merge) ловил его ОДНИМ
    # общим try вокруг всего вызова — сбой сети на repo-ci.yml (первый в
    # реестре) молча отменял диспатч worker-ci.yml/deploy-worker.yml, у
    # которых сети вполне могло хватить. Порядок реестра — repo-ci.yml,
    # codeql.yml, worker-ci.yml, deploy-worker.yml (см. REGISTRY выше):
    # первый матчащий cf-worker/ workflow с ошибкой — repo-ci.yml.
    fake = FakeGh({
        "workflows/repo-ci.yml/runs?head_sha=abc123": RuntimeError("gh api: rate limited"),
        "workflows/codeql.yml/runs?head_sha=abc123": {"workflow_runs": []},
        "workflows/worker-ci.yml/runs?head_sha=abc123": {"workflow_runs": []},
        "workflows/deploy-worker.yml/runs?head_sha=abc123": {"workflow_runs": []},
        "-X POST": None,
    })
    actions = mr.react_to_merge(
        fake, "o/r", files("cf-worker/src/config.ts"), "abc123", registry=REGISTRY)
    assert any("⚠️" in a and "repo-ci.yml" in a and "rate limited" in a for a in actions)
    posts = [c for c in fake.calls if c.startswith("-X POST")]
    # Остальные три workflow — БЕЗ сбоя — обязаны быть продиспатчены, сбой
    # первого их не заблокировал.
    assert any("codeql.yml/dispatches" in c for c in posts)
    assert any("worker-ci.yml/dispatches" in c for c in posts)
    assert any("deploy-worker.yml/dispatches" in c for c in posts)
    assert not any("repo-ci.yml/dispatches" in c for c in posts)  # сбой — не дублируем гаданием
