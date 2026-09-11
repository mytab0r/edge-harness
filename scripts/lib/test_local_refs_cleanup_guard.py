#!/usr/bin/env python3
"""Гвардия уборки локальных refs разработчика (#940): безопасность на
настоящем временном git-репозитории (tempfile + `git init`), не текст
исходника — переименование/вырезание проверки красит поведение, не имя.

Три класса, три набора сценариев:
  1. Локальные ветки agent/*: `branch_content_is_preserved` — доказательство
     сохранности ПЕРЕД тем, как ветка вообще считается кандидатом.
  2. Кэш-ссылки refs/remotes/pr/<N>: `pr_cache_refs_to_prune` — удаляем
     только когда PR закрыт/смёржен И sha доказуемо жив ещё где-то.
  3. Огрызки refs/tmp/*: `is_stray_lock_ref` — удаляем ТОЛЬКО опознанные по
     точному сообщению коммита lock-объекта claim_task.py, не любой refs/tmp/*.

Доказательство мутацией (ручной прогон, дословный вывод — в отчёте):
  - в `branch_content_is_preserved` заменить `return origin_sha == local_sha`
    на `return True` — `test_branch_with_unique_local_commit_is_not_preserved`
    красный: ветка с уникальным локальным коммитом считается сохранённой.
  - в `pr_cache_refs_to_prune` убрать проверку `status == "OPEN"` —
    `test_open_pr_cache_ref_is_kept` красный.
  - в `is_stray_lock_ref` заменить `LOCK_REF_MESSAGE_RE.match(subject)` на
    `True` — `test_unrelated_tmp_ref_is_not_touched` красный: любой чужой
    `refs/tmp/*` начинает считаться lock-огрызком.

Запуск: python -m pytest scripts/lib/test_local_refs_cleanup_guard.py -q
"""

import importlib.util
import subprocess
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "git" / "local-refs-cleanup.py"
_spec = importlib.util.spec_from_file_location("local_refs_cleanup", _SCRIPT_PATH)
lrc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lrc)  # type: ignore[union-attr]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {result.stdout}\n{result.stderr}")
    return result


def _setup_repo(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    work = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("init\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "HEAD:refs/heads/main")
    return work


# ── 1. branch_content_is_preserved ───────────────────────────────────────────


def test_branch_matching_origin_tip_is_preserved(tmp_path):
    work = _setup_repo(tmp_path)
    _git(work, "checkout", "-b", "agent/1-x")
    _git(work, "push", "origin", "agent/1-x")
    assert lrc.branch_content_is_preserved(str(work), "agent/1-x") is True


def test_branch_ancestor_of_main_after_origin_branch_gone_is_preserved(tmp_path):
    work = _setup_repo(tmp_path)
    _git(work, "checkout", "-b", "agent/2-x")
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "work")
    # "смёржено" в main (fast-forward имитирует то, что коммит теперь предок main)
    _git(work, "checkout", "main")
    _git(work, "merge", "--ff-only", "agent/2-x")
    _git(work, "push", "origin", "HEAD:refs/heads/main")
    # origin/agent/2-x никогда не заводили — сценарий "уже снят где-то ещё"
    assert lrc.branch_content_is_preserved(str(work), "agent/2-x") is True


def test_branch_diverged_from_origin_tip_is_not_preserved(tmp_path):
    # origin/<branch> ЖИВ, но указывает на СТАРЫЙ коммит — локально есть
    # новый коммит поверх, которого на origin нет: `origin/<branch>` есть
    # (rc_origin == 0), но это НЕ доказывает сохранность именно ТЕКУЩЕГО
    # локального состояния.
    work = _setup_repo(tmp_path)
    _git(work, "checkout", "-b", "agent/4-x")
    _git(work, "push", "origin", "agent/4-x")
    (work / "extra.txt").write_text("непушенный коммит поверх\n", encoding="utf-8")
    _git(work, "add", "extra.txt")
    _git(work, "commit", "-m", "локальный коммит, которого нет на origin")
    assert lrc.branch_content_is_preserved(str(work), "agent/4-x") is False


def test_branch_with_unique_local_commit_is_not_preserved(tmp_path):
    work = _setup_repo(tmp_path)
    _git(work, "checkout", "-b", "agent/3-x")
    (work / "unique.txt").write_text("только тут\n", encoding="utf-8")
    _git(work, "add", "unique.txt")
    _git(work, "commit", "-m", "работа, которую никуда не пушили")
    # origin/agent/3-x нет, и коммит НЕ предок origin/main
    assert lrc.branch_content_is_preserved(str(work), "agent/3-x") is False


# ── 2. pr_cache_refs_to_prune ─────────────────────────────────────────────────


def test_open_pr_cache_ref_is_kept(tmp_path):
    work = _setup_repo(tmp_path)
    head_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    deletable, kept = lrc.pr_cache_refs_to_prune(str(work), {5: head_sha}, {5: "OPEN"})
    assert deletable == []
    assert "открыт" in kept[5]


def test_merged_pr_cache_ref_with_sha_preserved_is_deletable(tmp_path):
    work = _setup_repo(tmp_path)
    head_sha = _git(work, "rev-parse", "HEAD").stdout.strip()  # предок origin/main тривиально
    deletable, kept = lrc.pr_cache_refs_to_prune(str(work), {6: head_sha}, {6: "MERGED"})
    assert deletable == [6]
    assert kept == {}


def test_closed_pr_cache_ref_with_unpreserved_sha_is_kept(tmp_path):
    work = _setup_repo(tmp_path)
    _git(work, "checkout", "-b", "agent/7-x")
    (work / "u.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "u.txt")
    _git(work, "commit", "-m", "не смёржено никуда")
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "checkout", "main")
    deletable, kept = lrc.pr_cache_refs_to_prune(str(work), {7: sha}, {7: "CLOSED"})
    assert deletable == []
    assert "не доказано" in kept[7]


def test_pr_cache_ref_without_pr_record_is_kept(tmp_path):
    work = _setup_repo(tmp_path)
    head_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    deletable, kept = lrc.pr_cache_refs_to_prune(str(work), {8: head_sha}, {})
    assert deletable == []
    assert "не знаем статуса" in kept[8]


# ── 3. is_stray_lock_ref ──────────────────────────────────────────────────────


def test_lock_commit_message_is_recognised_as_stray(tmp_path):
    work = _setup_repo(tmp_path)
    _git(work, "commit", "--allow-empty", "-m",
         "lock: task #572 claimed by unknown at 2026-09-07T12:20:41+00:00 (ttl 24h)")
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "update-ref", "refs/tmp/l572", sha)
    assert lrc.is_stray_lock_ref(str(work), "refs/tmp/l572") is True


def test_unrelated_tmp_ref_is_not_touched(tmp_path):
    work = _setup_repo(tmp_path)
    _git(work, "commit", "--allow-empty", "-m", "какая-то отладочная метка, не lock")
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "update-ref", "refs/tmp/mystery", sha)
    assert lrc.is_stray_lock_ref(str(work), "refs/tmp/mystery") is False


# ── Поведенческая проверка: удаление реально снимает ref ────────────────────


def test_delete_ref_and_delete_local_branch_and_delete_pr_cache_ref_actually_remove(tmp_path):
    work = _setup_repo(tmp_path)

    _git(work, "checkout", "-b", "agent/9-doomed")
    _git(work, "checkout", "main")
    assert lrc.delete_local_branch(str(work), "agent/9-doomed") is True
    branches = _git(work, "branch", "--list", "agent/9-doomed").stdout
    assert "agent/9-doomed" not in branches

    head_sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "update-ref", "refs/remotes/pr/42", head_sha)
    assert lrc.delete_pr_cache_ref(str(work), 42) is True
    assert lrc.local_pr_cache_refs(str(work)) == {}

    _git(work, "update-ref", "refs/tmp/scratch", head_sha)
    assert lrc.delete_ref(str(work), "refs/tmp/scratch") is True
    assert lrc.tmp_lock_refs(str(work)) == []
