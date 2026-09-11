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
  - в `branch_content_is_preserved` заменить `return remote_sha == local_sha`
    на `return True` — `test_branch_with_unique_local_commit_is_not_preserved`
    красный: ветка с уникальным локальным коммитом считается сохранённой.
  - в `branch_content_is_preserved` вернуть проверку по локальному кэшу
    (`git rev-parse origin/<branch>` вместо `git ls-remote --heads origin`,
    находка ревью PR #944, третий раунд) —
    `test_branch_with_stale_origin_tracking_ref_after_deletion_is_not_preserved`
    красный: протухший `refs/remotes/origin/<branch>` снова засчитывает
    снятую на GitHub ветку сохранённой.
  - в `pr_cache_refs_to_prune` убрать проверку `status == "OPEN"` —
    `test_open_pr_cache_ref_is_kept` красный.
  - в `sha_preserved_elsewhere` заменить `git for-each-ref --contains …
    refs/remotes/origin` обратно на общий `git branch -r --contains`
    (находка ревью PR #944, второй раунд: видит и сам `refs/remotes/pr/<N>`,
    поэтому «--contains» тривиально находит его самого) —
    `test_closed_pr_cache_ref_with_unpreserved_sha_is_kept` красный: закрытый
    PR без второй копии снова признаётся «сохранённым».
  - в `is_stray_lock_ref` заменить `LOCK_REF_MESSAGE_RE.match(subject)` на
    `True` — `test_unrelated_tmp_ref_is_not_touched` красный: любой чужой
    `refs/tmp/*` начинает считаться lock-огрызком.
  - в `git_listing`/листингах вернуть молчаливую пустоту при rc != 0
    (находка ревью PR #944: сбой отдавался сигналом успеха) —
    `test_git_listing_failure_raises_not_empty` и
    `test_main_returns_1_when_git_listing_fails` красные: отказ листинга
    снова пустой список и «Итого: 0» с кодом 0.
  - в `main()` вернуть `pr_records = []` вместо `return 1` при
    `fetch_pr_records() is None` (находка ревью PR #944, третий раунд) —
    `test_main_returns_1_and_no_itogo_when_gh_pr_list_fails` красный: код
    возврата снова 0 и «Итого: веток 0 …» печатается, хотя секции 1–2 не
    оценивались.

Запуск: python -m pytest scripts/lib/test_local_refs_cleanup_guard.py -q
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

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


def test_branch_with_stale_origin_tracking_ref_after_deletion_is_not_preserved(tmp_path, monkeypatch):
    # Живое репро находки ревью PR #944 (третий раунд): ветку сняли на
    # GitHub из другого места (не через этот клон — `git push --delete`
    # из ЭТОГО клона сам обновил бы локальный tracking-реф, маскируя баг),
    # локальный remote-tracking реф `refs/remotes/origin/<branch>` в этом
    # клоне остаётся живым (fetch --prune не запускали) и указывает на тот
    # же коммит — совпадение с ним не должно засчитываться сохранностью,
    # потому что живой проверки на GitHub (`ls-remote`) при этом нет.
    bare = tmp_path / "origin.git"
    work = _setup_repo(tmp_path)
    _git(work, "checkout", "-b", "agent/5-stale")
    (work / "u.txt").write_text("не смёржено и не сохранено\n", encoding="utf-8")
    _git(work, "add", "u.txt")
    _git(work, "commit", "-m", "непринятая работа")
    _git(work, "push", "origin", "agent/5-stale")
    _git(work, "fetch", "origin")  # заводит refs/remotes/origin/agent/5-stale локально
    # Ветку снимают на GitHub НЕ через этот клон (имитация: другой агент,
    # оркестратор, веб-интерфейс) — локальный tracking-реф клона не узнаёт
    # об этом без отдельного fetch --prune.
    _git(Path(str(bare)), "update-ref", "-d", "refs/heads/agent/5-stale")
    cache_check = _git(work, "rev-parse", "origin/agent/5-stale")
    assert cache_check.returncode == 0, "тест несостоятелен без живого локального кэша"
    assert lrc.branch_content_is_preserved(str(work), "agent/5-stale") is False


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
    # Прод-форма входа (находка ревью PR #944, второй раунд): реальный
    # refs/remotes/pr/7 создан в репозитории, не только передан словарём —
    # раньше `git branch -r --contains sha` видел ЭТОТ ЖЕ реф и признавал
    # sha «сохранённым в другом месте», хотя другого места не было.
    work = _setup_repo(tmp_path)
    _git(work, "checkout", "-b", "agent/7-x")
    (work / "u.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "u.txt")
    _git(work, "commit", "-m", "не смёржено никуда")
    sha = _git(work, "rev-parse", "HEAD").stdout.strip()
    _git(work, "checkout", "main")
    _git(work, "update-ref", "refs/remotes/pr/7", sha)
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


# ── Fail loud: отказ git-листинга — не пустой список ─────────────────────────


def test_git_listing_failure_raises_not_empty(tmp_path):
    # Настоящий git на НЕ-репозитории: rc != 0. Молчаливая пустота (прошлое
    # поведение листингов) печатала бы «Итого: веток 0 …» и выходила 0.
    with pytest.raises(lrc.GitListingError, match="not a git repository|rc=128"):
        lrc.local_agent_branches(str(tmp_path))
    with pytest.raises(lrc.GitListingError):
        lrc.tmp_lock_refs(str(tmp_path))
    with pytest.raises(lrc.GitListingError):
        lrc.local_pr_cache_refs(str(tmp_path))
    with pytest.raises(lrc.GitListingError):
        lrc.worktree_checked_out_branches(str(tmp_path))


def test_git_listing_success_with_empty_output_is_honest_empty(tmp_path):
    # Пустой вывод при rc == 0 — честный пустой список, не сбой: git-репозиторий
    # без refs/remotes/pr/* и без refs/tmp/*.
    work = _setup_repo(tmp_path)
    assert lrc.local_pr_cache_refs(str(work)) == {}
    assert lrc.tmp_lock_refs(str(work)) == []
    assert lrc.local_agent_branches(str(work)) == []


def test_main_returns_1_and_no_itogo_when_gh_pr_list_fails(tmp_path, monkeypatch, capsys):
    # Симметрично GitListingError: отказ `gh pr list` (секции 1–2 от него
    # зависят) обязан оборвать main() кодом 1 ДО печати «Итого» — находка
    # ревью PR #944, третий раунд (раньше секции 1–2 молча пропускались, но
    # «Итого: веток 0 …» с кодом 0 всё равно печаталось).
    work = _setup_repo(tmp_path)
    monkeypatch.chdir(work)
    monkeypatch.setattr(sys, "argv", ["local-refs-cleanup.py", "--dry-run"])
    monkeypatch.setattr(lrc.merged_branch_cleanup, "fetch_pr_records", lambda: None)
    rc = lrc.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert "gh pr list" in captured.err
    assert "Итого" not in captured.out
    assert "Итого" not in captured.err


def test_main_returns_1_when_git_listing_fails(tmp_path, monkeypatch, capsys):
    # Сквозной контракт main(): отказ листинга — причина в stderr, код 1,
    # БЕЗ обманчивого «Итого: веток 0 …».
    work = _setup_repo(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local-refs-cleanup.py", "--dry-run"])
    monkeypatch.setattr(lrc.merged_branch_cleanup, "fetch_pr_records", lambda: [])
    monkeypatch.setattr(
        lrc, "tmp_lock_refs",
        lambda root: (_ for _ in ()).throw(
            lrc.GitListingError("git for-each-ref refs/tmp/: rc=128: fatal: сломан refdb")))
    rc = lrc.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert "git for-each-ref refs/tmp/" in captured.err
    assert "Итого" not in captured.out
    assert "Итого" not in captured.err


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
