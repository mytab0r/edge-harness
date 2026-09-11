#!/usr/bin/env python3
"""Тесты scripts/lib/merge_reactions_registry_guard.py (issue #955).

Прод-форма: фикстуры workflow ниже — реальные фрагменты `on:` из
.github/workflows/*.yml этого репозитория (repo-ci.yml/codeql.yml/
worker-ci.yml/deploy-worker.yml), не пересказ. Мутация доказывает саму
цель гвардии (#929/#218): новый workflow с `on.push` по `main`, забытый в
реестре, обязан краснить.

Запуск: python -m pytest scripts/lib/test_merge_reactions_registry_guard.py -q
"""

import importlib.util
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "merge_reactions_registry_guard.py"
spec = importlib.util.spec_from_file_location("merge_reactions_registry_guard", SCRIPT)
grd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grd)  # type: ignore[union-attr]


def write_workflow(path: Path, on_block: str) -> None:
    path.write_text(f"name: test\n\non:\n{on_block}\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: []\n",
                     encoding="utf-8")


def write_registry(path: Path, reactions, excluded=()) -> None:
    path.write_text(json.dumps({"reactions": list(reactions), "excluded": list(excluded)}), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════
# push_main_workflows — распознавание on.push по main (включая готчу PyYAML)
# ══════════════════════════════════════════════════════════════════════════


def test_push_main_workflows_detects_bare_on_key_yaml11_gotcha(tmp_path):
    # PyYAML безопасный парсер трактует незакавыченный `on:` как булево True
    # (YAML 1.1) — доказано живым запуском на repo-ci.yml (issue #955).
    # Гвардия обязана читать оба ключа ('on' и True), не только один.
    write_workflow(tmp_path / "a.yml", "  push:\n    branches: [main]\n")
    assert grd.push_main_workflows(tmp_path) == {"a.yml"}


def test_push_main_workflows_bare_push_without_branches_covers_main(tmp_path):
    write_workflow(tmp_path / "b.yml", "  push:\n  pull_request:\n")
    assert grd.push_main_workflows(tmp_path) == {"b.yml"}


def test_push_main_workflows_excludes_branch_filtered_without_main(tmp_path):
    write_workflow(tmp_path / "c.yml", "  push:\n    branches: [staging]\n")
    assert grd.push_main_workflows(tmp_path) == set()


def test_push_main_workflows_ignores_workflow_dispatch_only(tmp_path):
    write_workflow(tmp_path / "d.yml", "  workflow_dispatch:\n")
    assert grd.push_main_workflows(tmp_path) == set()


def test_push_main_workflows_real_repo_ci_yml_detected():
    # Прод-форма: реальный файл репозитория, не фикстура.
    real_dir = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    result = grd.push_main_workflows(real_dir)
    assert {"repo-ci.yml", "codeql.yml", "worker-ci.yml",
            "deploy-worker.yml", "deploy-dsh-edge.yml"} <= result


# ══════════════════════════════════════════════════════════════════════════
# check_registry_completeness — мутация доказывает класс #929/#218
# ══════════════════════════════════════════════════════════════════════════


def test_check_registry_completeness_clean_on_real_repo_files():
    # Живой снимок на момент внедрения (#955) — ноль нарушений: гвардия не
    # красит собственное введение.
    assert grd.check_registry_completeness() == []


def test_new_push_main_workflow_missing_from_registry_is_flagged(tmp_path):
    """Мутация класса: новый workflow с on.push по main, забытый в реестре
    (никто не добавил ни reactions, ни excluded), — ровно тот случай,
    который #929 нашёл живьём для repo-ci.yml/codeql.yml/worker-ci.yml.
    Красный ДО фикса (нет записи), зелёный ПОСЛЕ (добавили в реестр) —
    обе стороны мутации ниже."""
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    write_workflow(workflows_dir / "new-push.yml", "  push:\n    branches: [main]\n")
    write_workflow(workflows_dir / "other.yml", "  workflow_dispatch:\n")
    registry_path = tmp_path / "merge-reactions.json"
    write_registry(registry_path, reactions=[{"prefix": "", "workflow": "other.yml"}])

    # До фикса: новый workflow не учтён нигде — гвардия красная.
    problems = grd.check_registry_completeness(workflows_dir, registry_path)
    assert any("new-push.yml" in p and "не зарегистрирован" in p for p in problems)

    # После фикса: добавили запись в excluded (или reactions) — зелёная.
    write_registry(
        registry_path,
        reactions=[{"prefix": "", "workflow": "other.yml"}],
        excluded=[{"workflow": "new-push.yml", "reason": "тестовое исключение"}],
    )
    assert grd.check_registry_completeness(workflows_dir, registry_path) == []


def test_dead_registry_entry_pointing_to_removed_workflow_is_flagged(tmp_path):
    """Мутация второго направления: реестр называет workflow, которого
    больше нет на диске (переименовали/удалили, реестр не обновили) —
    зависшая ссылка, тоже нарушение «одного места правды»."""
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    write_workflow(workflows_dir / "kept.yml", "  push:\n    branches: [main]\n")
    registry_path = tmp_path / "merge-reactions.json"
    write_registry(
        registry_path,
        reactions=[
            {"prefix": "", "workflow": "kept.yml"},
            {"prefix": "cf-worker/", "workflow": "removed-long-ago.yml"},
        ],
    )
    problems = grd.check_registry_completeness(workflows_dir, registry_path)
    assert any("removed-long-ago.yml" in p and "мёртвая запись" in p for p in problems)


def test_excluded_workflow_with_reason_does_not_flag(tmp_path):
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    write_workflow(workflows_dir / "forge.yml", "  push:\n    branches: [main]\n    paths: [plugins-src/**]\n")
    write_workflow(workflows_dir / "other.yml", "  workflow_dispatch:\n")
    registry_path = tmp_path / "merge-reactions.json"
    write_registry(
        registry_path, reactions=[{"prefix": "", "workflow": "other.yml"}],
        excluded=[{"workflow": "forge.yml", "reason": "динамические входы, см. #946"}],
    )
    assert grd.check_registry_completeness(workflows_dir, registry_path) == []


def test_main_exits_nonzero_when_registry_incomplete(tmp_path, monkeypatch, capsys):
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    write_workflow(workflows_dir / "orphan.yml", "  push:\n    branches: [main]\n")
    write_workflow(workflows_dir / "other.yml", "  workflow_dispatch:\n")
    registry_path = tmp_path / "merge-reactions.json"
    write_registry(registry_path, reactions=[{"prefix": "", "workflow": "other.yml"}])
    monkeypatch.setattr(grd, "WORKFLOWS_DIR", workflows_dir)
    monkeypatch.setattr(grd, "REGISTRY_PATH", registry_path)
    assert grd.main() == 1
    assert "::error::" in capsys.readouterr().out
