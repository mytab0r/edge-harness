#!/usr/bin/env python3
"""Retention для рабочих деревьев агентов под .claude/worktrees (#565).

Локальный инструмент гигиены диска, не шаг GitHub Actions: `.claude/worktrees/`
живёт только на диске владельца/агента, оркестратор GitHub (orchestra.yml)
работает на эфемерных раннерах и этот каталог не видит вообще. Триггер —
ручной или периодический локальный запуск (человек, локальная сессия
Claude Code), не GitHub-workflow.

Правило удаления — строго консервативное (AGENTS.md, «тормоз без газа не
принимается» здесь работает в обратную сторону: газ без тормоза — это
удаление чужой незакоммиченной работы, поэтому тормозов здесь ощутимо больше,
чем газа):

  1. Дерево вне `<repo>/.claude/worktrees/` мехнизм не видит вообще — сфера
     действия ограничена одним префиксом (REQUIRED: явный абсолютный путь,
     не собранный из переменных окружения; см. main()).
  2. Живой агент (ЕДИНСТВЕННОЕ место правды порога —
     LIVENESS_THRESHOLD_SECONDS ниже) — дерево не трогается НИКОГДА, даже
     если остальные условия выполнены. Признак — mtime самого свежего файла
     рабочей копии (метаданные git линкованного worktree лежат в
     `<repo>/.git/worktrees/<name>/`, НЕ внутри самого дерева, поэтому обычная
     работа git — checkout/commit/lock — не портит этот сигнал: mtime внутри
     дерева меняется, только когда кто-то пишет файлы рабочей копии).
  3. Дерево обязано быть чистым (`git status --porcelain` пусто).
  4. Ветка обязана называть задачу (`agent/<N>-<slug>`) и иметь PR, который
     либо слит (MERGED), либо закрыт без слияния (CLOSED) — открытый PR или
     отсутствие PR вообще не удаляются.
  5. Ветка не должна нести неотправленных коммитов: если `origin/<branch>`
     ещё существует — сверяем напрямую; если сервер уже удалил ветку (обычно
     после squash-мержа), для MERGED считаем безопасным только если HEAD —
     предок `origin/main` (то есть весь контент дерева уже целиком в main);
     для CLOSED без сохранившейся ветки на сервере проверить нечем — сомнение
     решается в пользу «не удалять» (AGENTS.md, «любое сомнение — не
     удалять»).

Сеть: только `gh pr list` (чтение). Скрипт НЕ делает `git fetch` сам —
свежесть `refs/remotes/origin/*` в общем `.git` уже поддерживают обычные
операции репозитория (`scripts/git/task-branch` делает fetch на каждый вход
в задачу) — держать отдельный источник фетча означало бы второе место
правды и скрытый сетевой побочный эффект инструмента для уборки. Стухший
локальный `origin/main` не портит безопасность: он только сокращает список
кандидатов на этот прогон (см. п.5), никогда не расширяет.

Удаление — исключительно `git worktree remove <path>` (без --force). Отказ
git remove (дерево оказалось не таким чистым, как показалось) — громкая
ошибка, не молчаливый пропуск.

Использование:
    python3 scripts/git/worktree_gc.py            # сухой прогон (по умолчанию)
    python3 scripts/git/worktree_gc.py --apply     # реальное удаление
Тонкая обёртка для единообразия с task-branch/pr-create: scripts/git/worktree-gc.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Одно место правды: порог «живой агент». Два часа — заведомо шире паузы
# между двумя ходами интерактивной сессии (ревью/раздумье/ожидание CI), но
# заведомо короче «дерево заброшено сутками». Раздел 2 docstring объясняет,
# почему mtime рабочей копии — надёжный сигнал именно для этого порога.
LIVENESS_THRESHOLD_SECONDS = 2 * 60 * 60

BRANCH_TASK_RE = re.compile(r"^agent/(\d+)-")

REMOVE = "REMOVE"
KEEP = "KEEP"


# Сетевые вызовы (gh) обязаны иметь таймаут: зависший gh (сеть/прокси)
# не должен вешать весь прогон навсегда — лучше громкая ошибка по одному
# дереву, чем тихое бесконечное ожидание по всем.
GH_TIMEOUT_SECONDS = 30


def run(args: list[str], cwd: str | Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"команда зависла дольше {timeout}с: {' '.join(args)}") from exc


@dataclass
class WorktreeEntry:
    path: Path
    branch: str | None  # None → detached HEAD


def list_worktrees(cwd: Path) -> list[WorktreeEntry]:
    """Все линкованные worktree общего репозитория (метаданные разделяются
    всеми worktree одного .git — можно запускать из любого из них)."""
    result = run(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"git worktree list упал: {result.stderr.strip()}")
    entries: list[WorktreeEntry] = []
    path: Path | None = None
    branch: str | None = None
    for line in result.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
            branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None
        elif line == "":
            if path is not None:
                entries.append(WorktreeEntry(path=path, branch=branch))
            path = None
    return entries


def find_repo_root(entries: list[WorktreeEntry]) -> Path:
    """Главное дерево — единственное, где .git каталог, а не файл-ссылка."""
    for entry in entries:
        if (entry.path / ".git").is_dir():
            return entry.path
    raise RuntimeError("не нашёл главное дерево репозитория среди worktree")


def most_recent_mtime(path: Path) -> float:
    latest = 0.0
    for root, dirs, files in os.walk(path):
        for name in files:
            try:
                mtime = os.path.getmtime(os.path.join(root, name))
            except OSError:
                continue
            if mtime > latest:
                latest = mtime
    return latest


def is_dirty(path: Path) -> bool:
    result = run(["git", "status", "--porcelain"], cwd=path)
    if result.returncode != 0:
        raise RuntimeError(f"git status упал в {path}: {result.stderr.strip()}")
    return bool(result.stdout.strip())


def pr_lookup(path: Path, branch: str) -> list[dict]:
    result = run(
        ["gh", "pr", "list", "--head", branch, "--state", "all",
         "--json", "number,state,url", "--limit", "10"],
        cwd=path, timeout=GH_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr list упал для {branch}: {result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def remote_branch_ref(path: Path, branch: str) -> str | None:
    result = run(["git", "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}"], cwd=path)
    return result.stdout.strip() if result.returncode == 0 else None


def unsent_commit_count(path: Path, base_ref: str) -> int:
    result = run(["git", "rev-list", "--count", f"{base_ref}..HEAD"], cwd=path)
    if result.returncode != 0:
        raise RuntimeError(f"git rev-list упал в {path}: {result.stderr.strip()}")
    return int(result.stdout.strip() or "0")


def is_ancestor_of_origin_main(path: Path) -> bool | None:
    """None — origin/main самого не нашлось локально (проверить нечем)."""
    if run(["git", "rev-parse", "--verify", "-q", "refs/remotes/origin/main"], cwd=path).returncode != 0:
        return None
    result = run(["git", "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main"], cwd=path)
    return result.returncode == 0


def decide(*, dirty: bool, live: bool, pr_state: str | None, unsent: bool | None) -> tuple[str, str]:
    """Чистая функция решения — без единого subprocess/gh, юнит-тестируется
    напрямую (scripts/git/test_worktree_gc.py). Оба «никогда» проверяются
    первыми и независимо друг от друга: порядок между ними не важен, важно,
    что НИ ОДИН из остальных фактов не может их перевесить.
    """
    if live:
        return KEEP, "живой агент (файлы менялись позже порога liveness)"
    if dirty:
        return KEEP, "незакоммиченные изменения в дереве"
    if pr_state is None:
        return KEEP, "PR для этой ветки не найден"
    if pr_state == "OPEN":
        return KEEP, "PR ещё открыт"
    if unsent is None:
        return KEEP, "не могу подтвердить отсутствие неотправленных коммитов"
    if unsent:
        return KEEP, "есть неотправленные/несмерженные коммиты"
    if pr_state in ("MERGED", "CLOSED"):
        return REMOVE, f"PR {pr_state.lower()}, дерево чистое, коммиты учтены"
    return KEEP, f"неизвестное состояние PR: {pr_state}"


def evaluate(path: Path, branch: str | None) -> tuple[str, str]:
    if branch is None:
        return KEEP, "detached HEAD — не задача-ветка"
    match = BRANCH_TASK_RE.match(branch)
    if not match:
        return KEEP, f"ветка «{branch}» не соответствует шаблону agent/<N>-<slug>"

    live = (time.time() - most_recent_mtime(path)) < LIVENESS_THRESHOLD_SECONDS
    if live:
        return decide(dirty=False, live=True, pr_state=None, unsent=None)

    dirty = is_dirty(path)
    if dirty:
        return decide(dirty=True, live=False, pr_state=None, unsent=None)

    prs = pr_lookup(path, branch)
    if not prs:
        return decide(dirty=False, live=False, pr_state=None, unsent=None)
    if any(pr["state"] == "OPEN" for pr in prs):
        return decide(dirty=False, live=False, pr_state="OPEN", unsent=None)
    merged = [pr for pr in prs if pr["state"] == "MERGED"]
    chosen = merged[0] if merged else max(prs, key=lambda pr: pr["number"])
    pr_state = chosen["state"]

    origin_ref = remote_branch_ref(path, branch)
    if origin_ref is not None:
        unsent = unsent_commit_count(path, f"refs/remotes/origin/{branch}") > 0
    elif pr_state == "MERGED":
        ancestor = is_ancestor_of_origin_main(path)
        unsent = None if ancestor is None else (not ancestor)
    else:
        unsent = None  # CLOSED без ветки на сервере — сомнение, не удаляем

    action, reason = decide(dirty=False, live=False, pr_state=pr_state, unsent=unsent)
    if action == REMOVE:
        reason = f"PR #{chosen['number']} {pr_state.lower()}, дерево чистое, коммиты учтены"
    return action, reason


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("Б", "КиБ", "МиБ", "ГиБ"):
        if size < 1024 or unit == "ГиБ":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГиБ"


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                         help="реально удалять (по умолчанию — сухой прогон)")
    args = parser.parse_args(argv)

    # Построчная буферизация: прогон по 60+ деревьям — минуты, и его могут
    # прервать (таймаут вызывающего, Ctrl-C). Без этого вывод, уже
    # логически напечатанный ДО прерывания, живёт только в буфере ОС и
    # теряется при убийстве процесса — то самое silent-wrong, когда
    # реальное удаление уже произошло, а видимого следа нет.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    script_dir = Path(__file__).resolve().parent
    entries = list_worktrees(script_dir)
    repo_root = find_repo_root(entries)
    # Явный абсолютный путь, вычисленный из git, а не собранный из переменных
    # окружения ($TEMP/%TEMP% сюда категорически не подставляются).
    scope = (repo_root / ".claude" / "worktrees").resolve()

    rows: list[tuple[Path, str, str]] = []
    for entry in entries:
        resolved = entry.path.resolve()
        if resolved == repo_root.resolve():
            continue
        try:
            resolved.relative_to(scope)
        except ValueError:
            continue  # вне области действия — не наш мандат
        try:
            action, reason = evaluate(resolved, entry.branch)
        except RuntimeError as exc:
            # Сбой одного дерева (сеть/gh/git) не должен ронять весь прогон —
            # но и не должен молча превращаться в решение: громкий KEEP.
            action, reason = KEEP, f"проверка сорвалась: {exc}"
        rows.append((resolved, action, reason))

    if not rows:
        print(f"Нет worktree в области действия ({scope}).")
        return 0

    freed_total = 0
    failures = 0
    for path, action, reason in rows:
        marker = "УДАЛИТЬ" if action == REMOVE else "оставить"
        print(f"[{marker}] {path}\n         причина: {reason}")
        if action == REMOVE and args.apply:
            size = dir_size(path)
            result = run(["git", "worktree", "remove", str(path)], cwd=repo_root)
            if result.returncode != 0:
                failures += 1
                print(f"::error::git worktree remove отказал для {path}: {result.stderr.strip()}", file=sys.stderr)
                continue
            freed_total += size
            print(f"         удалено, освобождено {human_size(size)}")

    to_remove = sum(1 for _p, a, _r in rows if a == REMOVE)
    if args.apply:
        print(f"\nУдалено деревьев: {to_remove - failures}/{to_remove}, освобождено {human_size(freed_total)}.")
        prune = run(["git", "worktree", "prune", "-v"], cwd=repo_root)
        if prune.stdout.strip():
            print(f"git worktree prune:\n{prune.stdout.strip()}")
    else:
        print(f"\nСухой прогон: {to_remove}/{len(rows)} деревьев были бы удалены. "
              f"Повтори с --apply для реального удаления.")
        prune = run(["git", "worktree", "prune", "-n", "-v"], cwd=repo_root)
        if prune.stdout.strip():
            print(f"git worktree prune (сухой прогон, уже отсутствующие каталоги):\n{prune.stdout.strip()}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
