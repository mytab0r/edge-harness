#!/usr/bin/env python3
"""Уборка мёртвых локальных git-refs на машине разработчика (#940).

Отдельный класс от `worktree-cleanup.py` (тот чистит каталоги
`.claude/worktrees/*`, этот — голые refs, которые остаются даже после того,
как рабочее дерево снято `git worktree remove`). Три вида мусора, замеренные
на машине владельца 2026-09-11:

  1. Локальные ветки `refs/heads/agent/*`, чей PR уже смерджен/закрыт (256
     на замер) — та же классификация, что `merged-branch-cleanup.py` уже
     держит для ORIGIN-веток (переиспользуем `branch_deletion_candidates`
     оттуда, тот же приоритет open > merged/closed, тот же retention), плюс
     ДОПОЛНИТЕЛЬНАЯ локальная проверка перед фактическим удалением: ветка не
     зачекаучена ни в одном worktree, и её содержимое доказуемо сохранено
     (совпадает с `origin/<ветка>`, если та ещё жива, либо `origin/<ветка>`
     уже снята нашей же отложенной уборкой И ветка при этом входит в предки
     `origin/main` — если ни то, ни другое не доказано, ветка НЕ трогается,
     даже если PR-статус разрешает: локальный коммит без доказанной копии
     нигде — то самое «не удаляй, что не доказано мёртвым»).
  2. Кэш-ссылки ревью `refs/remotes/pr/<N>` (297 на замер) — не рабочие
     ветки, а локальный кэш чужого PR (создан вручную `git fetch origin
     pull/N/head:refs/remotes/pr/N` или аналогом, штатный fetch-refspec
     репозитория — `+refs/heads/*:refs/remotes/origin/*` — их не касается).
     Удаляются, если PR закрыт/смёржен И его SHA доказуемо жив в другом
     месте (origin/main или любая живая ветка origin) — иначе не трогаются:
     нет второй копии — не факт мусора, факт «единственная сохранённая
     копия уже удалённой отовсюду ветки».
  3. Ссылки-огрызки `refs/tmp/*`, чей коммит несёт сообщение lock-объекта
     `claim_task.py` (`"lock: task #<N> claimed by ..."`, LOCK_REF_PREFIX —
     scripts/lib/claim_task.py:49/165) — локальная копия серверного
     `refs/locks/task-<N>`, снятая вручную для отладки аренды (живой пример
     — `refs/tmp/l572`). Сам серверный lock эфемерен по конструкции (TTL 24ч,
     claim_task.py release/expire) — локальная копия не несёт работы,
     удаляется безусловно по совпадению сообщения коммита; любой другой
     `refs/tmp/*` без этой сигнатуры НЕ трогается (неопознанный ref — не
     доказанный мусор).

Fail loud: не удалось определить безопасность (сеть недоступна для gh pr
list, `git rev-parse` не резолвится) — ref остаётся, скрипт печатает причину,
не гадает. То же для самих git-листингов (находка ревью PR #944, второй
раунд): отказ `git branch`/`git worktree list`/`for-each-ref` — сбой
инструмента, а не пустой список — GitListingError, причина в stderr,
код выхода 1, ничего не удалено; молчаливая пустота печатала бы
«Итого: веток 0 …» с кодом 0, отдавая сбой тем же сигналом, что успех.
Симметрично — отказ `gh pr list` (секции 1–2 зависят от него) обрывает
main() кодом 1 ДО печати «Итого» (находка ревью PR #944, третий раунд:
раньше при этом отказе секции 1–2 молча пропускались, но «Итого: веток 0…»
всё равно печаталось с кодом 0 — тот же класс лжи, что и с GitListingError,
не закрытый в первый заход).

Запуск (только вручную, на машине разработчика — не часть CI, эти refs в
свежем чекауте CI не существуют): `python scripts/git/local-refs-cleanup.py
[--dry-run]`.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

_MBC_SPEC = importlib.util.spec_from_file_location(
    "merged_branch_cleanup", Path(__file__).resolve().parent / "merged-branch-cleanup.py")
merged_branch_cleanup = importlib.util.module_from_spec(_MBC_SPEC)
_MBC_SPEC.loader.exec_module(merged_branch_cleanup)  # type: ignore[union-attr]

LOCK_REF_MESSAGE_RE = re.compile(r"^lock: task #\d+ claimed by ")


class GitListingError(RuntimeError):
    """Отказ git-листинга (rc != 0) — сбой инструмента, не пустой список
    (см. «Fail loud» в докстринге модуля)."""


def run_cmd(args: list[str], cwd: Optional[str] = None) -> tuple[str, int]:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip(), result.returncode


def git_listing(args: list[str], cwd: str) -> str:
    """git-листинг для КЛАССИФИКАЦИИ (что вообще существует): rc != 0 —
    GitListingError с командой и stderr; пустой вывод при rc == 0 — честный
    пустой список. Точечные git-пробы («жив ли origin/<branch>», «предок ли
    main») остаются на run_cmd: там НЕудачный rc — содержательный ответ
    («объекта нет»), а не сбой листинга."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise GitListingError(
            f"git {' '.join(args)}: rc={result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


# ── 1. Локальные ветки agent/*, чей PR уже смёржен/закрыт ────────────────────


def local_agent_branches(repo_root: str) -> list[str]:
    out = git_listing(["git", "branch", "--list", "agent/*", "--format=%(refname:short)"], cwd=repo_root)
    return [line.strip() for line in out.splitlines() if line.strip()]


def worktree_checked_out_branches(repo_root: str) -> set[str]:
    out = git_listing(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    branches = set()
    for line in out.splitlines():
        if line.startswith("branch refs/heads/"):
            branches.add(line[len("branch refs/heads/"):])
    return branches


def branch_content_is_preserved(repo_root: str, branch: str) -> bool:
    """Содержимое локальной ветки доказуемо сохранено где-то ещё: либо
    origin/<branch> ещё жив НА GITHUB и указывает на тот же коммит, либо
    origin/<branch> уже снят (наша же отложенная уборка, #940), но коммит —
    предок origin/main. Ни то ни другое не доказано — False (не трогаем,
    класс «локальный коммит без копии — не мусор»).

    Живость origin/<branch> проверяется напрямую на GitHub (`git ls-remote
    --heads origin`), НЕ по локальному remote-tracking рефу
    `refs/remotes/origin/<branch>`: тот не обновляется без `fetch --prune` и
    может остаться в клоне разработчика после того, как ветка снята на
    GitHub — совпадение с протухшим кэшем ложно засчитывало бы сохранность
    (находка ревью PR #944, третий раунд, живое репро: PR закрыт без мержа,
    origin-ветка снята, `refs/remotes/origin/<branch>` остался от старого
    fetch — `git rev-parse` по нему успешен и врёт, что ветка ещё на origin)."""
    local_sha, rc = run_cmd(["git", "rev-parse", branch], cwd=repo_root)
    if rc != 0:
        return False
    remote_out, rc_remote = run_cmd(
        ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=repo_root)
    if rc_remote == 0 and remote_out.strip():
        remote_sha = remote_out.split()[0]
        return remote_sha == local_sha
    _, rc_anc = run_cmd(["git", "merge-base", "--is-ancestor", branch, "origin/main"], cwd=repo_root)
    return rc_anc == 0


def delete_local_branch(repo_root: str, branch: str) -> bool:
    _, rc = run_cmd(["git", "branch", "-D", branch], cwd=repo_root)
    return rc == 0


# ── 2. Кэш-ссылки ревью refs/remotes/pr/<N> ───────────────────────────────────


def local_pr_cache_refs(repo_root: str) -> dict[int, str]:
    """{номер PR: sha} по refs/remotes/pr/<N>."""
    out = git_listing(["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/pr/"], cwd=repo_root)
    result = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        ref, sha = line.split(" ", 1)
        name = ref[len("refs/remotes/pr/"):]
        if name.isdigit():
            result[int(name)] = sha
    return result


def sha_preserved_elsewhere(repo_root: str, sha: str) -> bool:
    """SHA доказуемо жив в другом месте: предок origin/main ИЛИ достижим
    хотя бы с одной ЖИВОЙ ветки origin/*. Второе нужно отдельно от
    merge-base --is-ancestor origin/main, потому что squash-merge не делает
    голову PR предком main — единственное надёжное «жив» для НЕ смёрженной
    (closed) ветки — она всё ещё существует на origin.

    Проверка ограничена поддеревом `refs/remotes/origin` (`git for-each-ref
    --contains`), НЕ общим `git branch -r --contains`/`refs/remotes/*`:
    последний видит и сам проверяемый `refs/remotes/pr/<N>` — тот указывает
    на этот же sha, поэтому «--contains» тривиально находит его самого, и
    проверка вырождалась в «всегда True» для любого закрытого PR (находка
    ревью PR #944, второй раунд). `refs/remotes/pr/*` — наш собственный кэш,
    не вторая копия «где-то ещё»."""
    _, rc = run_cmd(["git", "merge-base", "--is-ancestor", sha, "origin/main"], cwd=repo_root)
    if rc == 0:
        return True
    out, rc_branch = run_cmd(
        ["git", "for-each-ref", "--contains", sha, "--format=%(refname)", "refs/remotes/origin"],
        cwd=repo_root)
    return rc_branch == 0 and bool(out.strip())


def pr_cache_refs_to_prune(
    repo_root: str,
    pr_cache: dict[int, str],
    pr_status_by_number: dict[int, str],
) -> tuple[list[int], dict[int, str]]:
    """(номера к удалению, {номер: причина оставить})."""
    deletable = []
    kept = {}
    for number, sha in pr_cache.items():
        status = pr_status_by_number.get(number)
        if status is None:
            kept[number] = "PR не найден в выдаче gh — не знаем статуса"
            continue
        if status == "OPEN":
            kept[number] = "PR ещё открыт"
            continue
        if sha_preserved_elsewhere(repo_root, sha):
            deletable.append(number)
        else:
            kept[number] = "PR закрыт, но SHA не найден нигде ещё — не доказано, что это не последняя копия"
    return deletable, kept


def delete_pr_cache_ref(repo_root: str, number: int) -> bool:
    _, rc = run_cmd(["git", "update-ref", "-d", f"refs/remotes/pr/{number}"], cwd=repo_root)
    return rc == 0


# ── 3. Огрызки refs/tmp/* — локальные копии lock-объектов claim_task.py ──────


def tmp_lock_refs(repo_root: str) -> list[str]:
    out = git_listing(["git", "for-each-ref", "--format=%(refname)", "refs/tmp/"], cwd=repo_root)
    return [line.strip() for line in out.splitlines() if line.strip()]


def is_stray_lock_ref(repo_root: str, ref: str) -> bool:
    subject, rc = run_cmd(["git", "log", "-1", "--format=%s", ref], cwd=repo_root)
    return rc == 0 and bool(LOCK_REF_MESSAGE_RE.match(subject))


def delete_ref(repo_root: str, ref: str) -> bool:
    _, rc = run_cmd(["git", "update-ref", "-d", ref], cwd=repo_root)
    return rc == 0


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root, rc = run_cmd(["git", "rev-parse", "--show-toplevel"])
    if rc != 0:
        print("🚨 не в git-репозитории", file=sys.stderr)
        return 1

    pr_records = merged_branch_cleanup.fetch_pr_records()
    if pr_records is None:
        # Симметрично merged-branch-cleanup.py::main и GitListingError ниже:
        # секции 1–2 не оценены вовсе, «Итого: веток 0 …» с кодом 0 отдавало
        # бы этот отказ инструмента тем же сигналом, что честный пустой
        # список (находка ревью PR #944, третий раунд) — выходим сразу, не
        # печатая «Итого».
        print("🚨 gh pr list не удался — уборка веток/pr-кэша пропущена (fail loud)", file=sys.stderr)
        return 1

    removed = {"branches": 0, "pr_refs": 0, "tmp_refs": 0}

    try:
        # 1. Локальные ветки agent/*
        checked_out = worktree_checked_out_branches(repo_root)
        local_branches = [b for b in local_agent_branches(repo_root) if b not in checked_out]
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        deletable, _kept = merged_branch_cleanup.branch_deletion_candidates(local_branches, pr_records, now)
        for branch in deletable:
            if not branch_content_is_preserved(repo_root, branch):
                print(f"ОСТАВЛЕНА {branch}: содержимое не доказано сохранённым нигде ещё")
                continue
            if args.dry_run:
                print(f"DRY-RUN: удалил бы локальную ветку {branch}")
                removed["branches"] += 1
                continue
            if delete_local_branch(repo_root, branch):
                print(f"Удалена локальная ветка: {branch}")
                removed["branches"] += 1
            else:
                print(f"ОШИБКА: не удалось удалить ветку {branch}", file=sys.stderr)

        # 2. Кэш-ссылки ревью refs/remotes/pr/<N>
        pr_status_by_number = {p["number"]: p["state"] for p in pr_records if "number" in p}
        pr_cache = local_pr_cache_refs(repo_root)
        deletable_pr_refs, _kept_pr = pr_cache_refs_to_prune(repo_root, pr_cache, pr_status_by_number)
        for number in deletable_pr_refs:
            if args.dry_run:
                print(f"DRY-RUN: удалил бы refs/remotes/pr/{number}")
                removed["pr_refs"] += 1
                continue
            if delete_pr_cache_ref(repo_root, number):
                print(f"Удалена refs/remotes/pr/{number}")
                removed["pr_refs"] += 1

        # 3. refs/tmp/* — только опознанные lock-огрызки
        for ref in tmp_lock_refs(repo_root):
            if not is_stray_lock_ref(repo_root, ref):
                print(f"ОСТАВЛЕН {ref}: не опознан как lock-огрызок claim_task.py — не трогаем")
                continue
            if args.dry_run:
                print(f"DRY-RUN: удалил бы {ref}")
                removed["tmp_refs"] += 1
                continue
            if delete_ref(repo_root, ref):
                print(f"Удалён {ref}")
                removed["tmp_refs"] += 1
    except GitListingError as error:
        # Классификация невозможна — «Итого» с нулями было бы ложью (сбой,
        # отданный сигналом успеха). Причина в stderr, код 1, ничего не удалено.
        print(f"🚨 git-листинг не удался — классификация невозможна, ничего не удалено (fail loud): {error}",
              file=sys.stderr)
        return 1

    print(f"Итого: веток {removed['branches']}, pr-кэша {removed['pr_refs']}, tmp-огрызков {removed['tmp_refs']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
