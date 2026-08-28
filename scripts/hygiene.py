#!/usr/bin/env python3
"""Гигиена машины и репозитория edge-harness.

Запуск: python scripts/hygiene.py [--docker]

Что делает:
  1. git fetch --prune — убирает протухшие remote-ссылки.
  2. Удаляет ЛОКАЛЬНЫЕ ветки, уже влитые в origin/main (кроме main и текущей).
  3. Сообщает о локальных ветках, не влитых никуда (их не удаляет — там работа).
  4. Убивает осиротевшие процессы workerd (рантайм wrangler dev — одноразовый по
     природе; живой dev-сервер проще поднять заново, чем искать зомби).
  5. С --docker — удаляет контейнер edge-harness-dev, если остановлен.

Не трогает: незакоммиченную работу, невлитые ветки, чужие процессы кроме workerd.
"""

import argparse
import os
import subprocess
import sys

KEEP = {"main"}


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])}: {result.stderr.strip()}")
    return result


def lines_of(result: subprocess.CompletedProcess) -> list[str]:
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_branches(repo: str) -> None:
    run("git", "fetch", "--prune")
    print("== Ветки ==")

    current = run("git", "branch", "--show-current").stdout.strip()
    # Обычный --merged не видит squash-мержи (коммиты ветки не предки main),
    # поэтому ветки влитых PR берём из gh — их содержимое уже в main.
    merged_pr_heads = set(
        lines_of(run("gh", "pr", "list", "--state", "merged", "--json", "headRefName",
                     "--jq", ".[].headRefName"))
    )
    plain_merged = set(
        b.lstrip("* ").split()[0]
        for b in lines_of(run("git", "branch", "--merged", "origin/main"))
    )

    removed = 0
    for branch in lines_of(run("git", "branch", "--format=%(refname:short)")):
        if branch in KEEP or branch == current:
            continue
        if branch in plain_merged or branch in merged_pr_heads:
            run("git", "branch", "-D", branch)
            print(f"  🧹 локальная ветка {branch} удалена (PR влит)")
            removed += 1
        else:
            print(f"  ⚠️ {branch}: не влита никуда — не трогаю")
    if removed == 0:
        print("  чисто: лишних локальных веток нет")

    unmerged = [
        b for b in lines_of(run("git", "branch", "--no-merged", "origin/main"))
        if b.lstrip("* ").split()[0] not in KEEP
    ]
    if unmerged:
        print("  ⚠️ невлитые ветки (там работа — не трогаю):")
        for b in unmerged:
            print(f"    {b}")


def processes() -> None:
    print("== Процессы ==")
    if sys.platform != "win32":
        print("  на не-Windows хосте workerd живёт в контейнере или под dev-менеджером")
        return
    result = run("tasklist", "/FI", "IMAGENAME eq workerd.exe", check=False)
    count = result.stdout.count("workerd.exe")
    if count:
        run("taskkill", "/F", "/IM", "workerd.exe", check=False)
        print(f"  🧹 убито осиротевших workerd: {count}")
    else:
        print("  чисто: workerd не запущен")


def docker_clean() -> None:
    print("== Docker ==")
    result = run("docker", "ps", "-aq", "--filter", "name=edge-harness-dev", check=False)
    container = result.stdout.strip()
    if container:
        run("docker", "rm", "-f", container)
        print("  🧹 контейнер edge-harness-dev удалён")
    else:
        print("  чисто: контейнера нет")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", help="также убрать контейнер edge-harness-dev")
    parser.add_argument("--no-processes", action="store_true", help="не трогать workerd")
    args = parser.parse_args()

    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    git_branches(repo)
    if not args.no_processes:
        processes()
    if args.docker:
        docker_clean()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::hygiene: {error}", file=sys.stderr)
        sys.exit(1)
