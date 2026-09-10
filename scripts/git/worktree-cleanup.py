#!/usr/bin/env python3
"""
Автоуборка мёртвых worktree'ов (слитые/закрытые PR без работы).

Retention story: worktree живёт до 24 часов после слияния PR, затем снимается.
Цель: освободить диск, не потеряв активную работу.

Безопасность:
  - Никогда не удаляет дерево с незакоммиченными изменениями
  - Никогда не удаляет дерево с локальными коммитами
  - Никогда не удаляет дерево открытого PR
  - Fail loud вместо молчаливого удаления

Условия удаления (ВСЕ должны быть выполнены):
  1. Ветка не существует на origin (обычно означает слияние)
  2. Нет незакоммиченных изменений (git status --porcelain пусто)
  3. Нет локальных коммитов впереди origin (git log @{u}..)
  4. Дерево старше 1 часа (retention) или если удалена целевая ветка
  5. По опции --force пропускается проверка на PR status (только для отладки)
"""

import subprocess
import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple, List

# Bootstrap UTF8 на Windows
try:
    from lib.console_utf8 import init_console_utf8
    init_console_utf8()
except ImportError:
    pass


def run_cmd(cmd: str, cwd: Optional[str] = None, check: bool = False) -> Tuple[str, int]:
    """Выполнить команду, вернуть (output, returncode)"""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            shell=True,
            timeout=30
        )
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"Command timeout: {cmd}")
        return "TIMEOUT", 1
    except Exception as e:
        if check:
            raise RuntimeError(f"Command failed: {cmd}\n{e}")
        return str(e), 1


class WorktreeAnalyzer:
    def __init__(self, repo_root: str, force: bool = False, verbose: bool = False):
        self.repo_root = repo_root
        self.force = force
        self.verbose = verbose
        self.retention_hours = 1

    def get_worktrees(self) -> List[dict]:
        """Получить список всех worktree'ов"""
        output, rc = run_cmd("git worktree list --porcelain", cwd=self.repo_root)
        if rc != 0:
            raise RuntimeError(f"Failed to list worktrees: {output}")

        worktrees = []
        current = {}

        for line in output.split('\n'):
            if line.startswith('worktree '):
                if current:
                    worktrees.append(current)
                current = {'path': line.replace('worktree ', '').strip()}
            elif line.startswith('HEAD '):
                current['head'] = line.replace('HEAD ', '').strip()
            elif line.startswith('branch '):
                current['branch'] = line.replace('branch ', '').strip()
            elif line.startswith('detached'):
                current['branch'] = 'DETACHED'

        if current:
            worktrees.append(current)

        # Filter to task worktrees only
        return [
            w for w in worktrees
            if '.claude/worktrees' in w.get('path', '')
        ]

    def branch_name(self, branch_ref: str) -> str:
        """Извлечь имя ветки из ref"""
        if branch_ref.startswith('refs/heads/'):
            return branch_ref.replace('refs/heads/', '')
        return branch_ref

    def check_dirty(self, worktree_path: str) -> bool:
        """Проверить наличие незакоммиченных изменений"""
        output, rc = run_cmd('git status --porcelain', cwd=worktree_path)
        if rc != 0:
            return True  # Если не смогли проверить — считаем грязным
        return bool(output.strip())

    def check_unpushed_commits(self, worktree_path: str) -> bool:
        """Проверить наличие локальных коммитов впереди origin"""
        output, rc = run_cmd('git rev-list --count @{u}.. 2>/dev/null', cwd=worktree_path)
        if rc != 0:
            return True  # Если не смогли проверить — считаем опасным
        try:
            count = int(output.strip() or 0)
            return count > 0
        except ValueError:
            return True

    def branch_exists_on_origin(self, worktree_path: str, branch: str) -> bool:
        """Проверить, существует ли ветка на origin"""
        output, rc = run_cmd(
            f'git rev-parse --verify origin/{branch} 2>/dev/null',
            cwd=worktree_path
        )
        return rc == 0

    def get_worktree_age_hours(self, worktree_path: str) -> float:
        """Получить возраст worktree'а в часах (по времени последнего доступа)"""
        try:
            stat = os.stat(worktree_path)
            age_seconds = datetime.now().timestamp() - stat.st_mtime
            return age_seconds / 3600
        except:
            return 0

    def get_pr_status(self, branch: str) -> Optional[str]:
        """Получить статус PR по ветке (merged/closed/open/not-found)"""
        if not branch.startswith('agent/'):
            return None

        # Извлечь номер задачи
        match = re.match(r'agent/(\d+)-', branch)
        if not match:
            return None

        task_num = match.group(1)

        # Поиск PR по номеру задачи в ветке
        cmd = f'gh pr list --state all --search "head:agent/{task_num}" --json state --jq ".[0].state" 2>/dev/null'
        output, rc = run_cmd(cmd, cwd=self.repo_root)

        if rc == 0 and output:
            return output.lower()

        return None

    def can_remove_worktree(self, worktree_info: dict) -> Tuple[bool, str]:
        """
        Проверить, безопасно ли удалять worktree.
        Возвращает (can_remove, reason).
        """
        path = worktree_info['path']
        branch = self.branch_name(worktree_info.get('branch', 'unknown'))

        # Проверка 1: наличие незакоммиченных изменений
        if self.check_dirty(path):
            return False, "Has uncommitted changes"

        # Проверка 2: наличие локальных коммитов
        if self.check_unpushed_commits(path):
            return False, "Has unpushed commits"

        # Проверка 3: ветка существует на origin?
        if self.branch_exists_on_origin(path, branch):
            return False, "Branch still exists on origin (not merged)"

        # Проверка 4: retention (дерево достаточно старое)
        age_hours = self.get_worktree_age_hours(path)
        if age_hours < self.retention_hours:
            return False, f"Too young (age: {age_hours:.1f}h < {self.retention_hours}h retention)"

        # Проверка 5 (опционально, если не --force): статус PR
        if not self.force:
            pr_status = self.get_pr_status(branch)
            if pr_status == 'open':
                return False, "Associated PR is still open"

        return True, "Safe to remove"

    def remove_worktree(self, worktree_path: str) -> bool:
        """Удалить worktree, вернуть True если успешно"""
        try:
            output, rc = run_cmd(f'git worktree remove "{worktree_path}"', cwd=self.repo_root)
            if rc != 0:
                print(f"ERROR: Failed to remove {worktree_path}: {output}", file=sys.stderr)
                return False
            return True
        except Exception as e:
            print(f"ERROR: Exception removing {worktree_path}: {e}", file=sys.stderr)
            return False

    def analyze_and_cleanup(self) -> dict:
        """Анализировать и очистить worktree'ы, вернуть статистику"""
        worktrees = self.get_worktrees()

        stats = {
            'total': len(worktrees),
            'removed': 0,
            'kept': 0,
            'dirty': [],
            'young': [],
            'no_origin': [],
            'unpushed': [],
            'open_pr': [],
            'errors': []
        }

        for wt in worktrees:
            path = wt['path']
            branch = self.branch_name(wt.get('branch', 'unknown'))

            can_remove, reason = self.can_remove_worktree(wt)

            if can_remove:
                if self.verbose:
                    print(f"Removing: {branch}")
                if self.remove_worktree(path):
                    stats['removed'] += 1
                else:
                    stats['errors'].append((branch, "Remove failed"))
            else:
                stats['kept'] += 1
                if self.verbose:
                    print(f"Keeping: {branch} ({reason})")

                # Categorize the reason
                if "uncommitted" in reason:
                    stats['dirty'].append(branch)
                elif "unpushed" in reason:
                    stats['unpushed'].append(branch)
                elif "still exists" in reason:
                    stats['no_origin'].append(branch)
                elif "young" in reason:
                    stats['young'].append(branch)
                elif "still open" in reason:
                    stats['open_pr'].append(branch)

        return stats

    def print_summary(self, stats: dict):
        """Вывести сводку"""
        print(f"\n=== WORKTREE CLEANUP SUMMARY ===")
        print(f"Total worktrees: {stats['total']}")
        print(f"Removed: {stats['removed']}")
        print(f"Kept: {stats['kept']}")

        if stats['dirty']:
            print(f"\nDirty (uncommitted): {len(stats['dirty'])}")
            for b in stats['dirty'][:5]:
                print(f"  - {b}")
            if len(stats['dirty']) > 5:
                print(f"  ... and {len(stats['dirty']) - 5} more")

        if stats['unpushed']:
            print(f"\nUnpushed commits: {len(stats['unpushed'])}")
            for b in stats['unpushed'][:5]:
                print(f"  - {b}")
            if len(stats['unpushed']) > 5:
                print(f"  ... and {len(stats['unpushed']) - 5} more")

        if stats['young']:
            print(f"\nToo young (retention): {len(stats['young'])}")
            for b in stats['young'][:3]:
                print(f"  - {b}")
            if len(stats['young']) > 3:
                print(f"  ... and {len(stats['young']) - 3} more")

        if stats['open_pr']:
            print(f"\nOpen PR (not merged): {len(stats['open_pr'])}")
            for b in stats['open_pr'][:3]:
                print(f"  - {b}")
            if len(stats['open_pr']) > 3:
                print(f"  ... and {len(stats['open_pr']) - 3} more")

        if stats['errors']:
            print(f"\nErrors: {len(stats['errors'])}")
            for branch, error in stats['errors']:
                print(f"  - {branch}: {error}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Skip PR status check (debug only)'
    )
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        help='Verbose output'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Analyze only, do not remove'
    )

    args = parser.parse_args()

    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', '..')
    )

    try:
        analyzer = WorktreeAnalyzer(repo_root, force=args.force, verbose=args.verbose)

        if args.dry_run:
            print("DRY RUN MODE - No worktrees will be removed\n")
            # Temporarily disable removal
            original_remove = analyzer.remove_worktree
            analyzer.remove_worktree = lambda path: True  # Mock removal

        stats = analyzer.analyze_and_cleanup()
        analyzer.print_summary(stats)

        # Exit with non-zero if there were errors
        if stats['errors']:
            sys.exit(1)

    except Exception as e:
        print(f"FATAL ERROR: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
