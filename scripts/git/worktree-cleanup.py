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
  1. Нет незакоммиченных изменений (git status --porcelain пусто)
  2. Нет локальных коммитов, которых нет больше нигде (git rev-list --count
     @{u}.., с запасным путём через origin/main, если апстрим-ветка уже
     упразднена на origin и вычищена локальным fetch --prune)
  3. Associated PR не в статусе open (см. ниже про сигнал "жива ли ветка")
  4. Дерево старше retention_hours (по умолчанию 1 час, инъекция параметром —
     не жёсткая константа, чтобы тесты не зависели от системных часов)
  5. По опции --force пропускается ТОЛЬКО проверка 3 (статус PR, debug-путь);
     пункты 1, 2, 4 обязательны всегда, --force их не отменяет

Почему сигнал не "ветка удалена на origin" (замер #891, живой прогон на
edge-harness): в этом репозитории оркестратор сливает PR, но НЕ удаляет
ветку на origin — из 128 живых деревьев 127 показывали "ветка ещё есть на
origin", включая ветки PR, смердженных месяцами ранее. Проверка по факту
присутствия ветки на origin делала бы уборку бессмысленной для этого
репозитория (0 кандидатов навсегда) — авторитетный сигнал "работа ещё не
закончена" здесь только статус PR (gh pr list), не факт существования ветки.
"""

import subprocess
import os
import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, List

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---


def run_cmd(cmd: str, cwd: Optional[str] = None, check: bool = False) -> Tuple[str, int]:
    """Выполнить команду, вернуть (output, returncode)"""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
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
    def __init__(
        self,
        repo_root: str,
        force: bool = False,
        verbose: bool = False,
        retention_hours: float = 1,
        now_ts: Optional[float] = None,
    ):
        self.repo_root = repo_root
        self.force = force
        self.verbose = verbose
        self.retention_hours = retention_hours
        # Инъекция «текущего времени» (не time.time()/datetime.now() напрямую
        # внутри get_worktree_age_hours) — тесты передают now_ts явно, чтобы
        # не зависеть от скорости выполнения и системных часов (класс
        # «тесты-бомбы», AGENTS.md). None — боевой путь, берём реальное время.
        self.now_ts = now_ts
        # Кэш статусов PR — один batched запрос на весь прогон, не один
        # запрос на дерево (класс: раньше get_pr_status дёргал `gh pr list`
        # ПЕРСОНАЛЬНО на каждое дерево; после того как PR-статус стал
        # обязательным гейтом почти для всех кандидатов — это десятки/сотни
        # последовательных сетевых вызовов на один прогон, минуты и квота
        # API. Один пакетный запрос ~5с против сотен по ~1с, замер #891).
        self._pr_status_cache: Optional[dict] = None
        self._pr_status_cache_ok: bool = False

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
        """Проверить наличие локальных коммитов, которых нет больше нигде.

        Без "2>/dev/null" — run_cmd уже вызывается с capture_output=True
        (stderr идёт в result.stderr, не на консоль), а сама редирекция вида
        "2>/dev/null" ломает команду под shell=True на нативном Windows
        cmd.exe (нет /dev/null): rc становится ненулевым ВСЕГДА, и это
        дерево навсегда считается "опасным" (баг найден поведенческим
        тестом на реальном git-репозитории, не текстовой гвардией, #891).
        """
        output, rc = run_cmd('git rev-list --count @{u}..', cwd=worktree_path)
        if rc == 0:
            try:
                return int(output.strip() or 0) > 0
            except ValueError:
                return True
        # @{u} не резолвится — типичный случай: апстрим-ветка уже удалена на
        # origin и локальная remote-tracking ссылка вычищена `git fetch
        # --prune` (ровно так и происходит после слияния PR). Без этого
        # запасного пути check_unpushed_commits возвращал(а) True для КАЖДОГО
        # дерева слитой ветки навсегда — скрипт никогда ничего не удалял
        # (баг найден поведенческим тестом на реальном репозитории, #891, а
        # не текстовой гвардией). Апстрима больше нет — сверяем HEAD дерева
        # напрямую с origin/main: если он уже есть в истории main, коммиты
        # никуда не потеряются при удалении дерева.
        _, rc_main = run_cmd('git merge-base --is-ancestor HEAD origin/main', cwd=worktree_path)
        if rc_main == 0:
            return False
        return True  # Не смогли доказать безопасность — считаем опасным

    def get_worktree_age_hours(self, worktree_path: str) -> float:
        """Получить возраст worktree'а в часах (по времени последнего доступа)"""
        try:
            stat = os.stat(worktree_path)
            now = self.now_ts if self.now_ts is not None else datetime.now().timestamp()
            age_seconds = now - stat.st_mtime
            return age_seconds / 3600
        except:
            return 0

    def _load_pr_status_cache(self) -> None:
        """Один пакетный `gh pr list --state all` на весь прогон (не на
        дерево) — см. комментарий в __init__. `--json headRefName,state`,
        сверка ТОЧНЫМ именем ветки (не `--search "head:<префикс>"`: GitHub
        `head:` в `--search` матчит ПОДСТРОКОЙ — живой замер #891, `--search
        "head:agent/1"` вернул 30 посторонних веток agent/170-…/agent/140-…/
        agent/131-… и т. д., ни одна не agent/1-*)."""
        cmd = 'gh pr list --state all --json headRefName,state --limit 2000'
        output, rc = run_cmd(cmd, cwd=self.repo_root)
        cache: dict = {}
        ok = False
        if rc == 0 and output:
            try:
                for item in json.loads(output):
                    ref = item.get('headRefName')
                    state = item.get('state')
                    if ref and state:
                        cache[ref] = state.lower()
                ok = True
            except (ValueError, TypeError, AttributeError):
                ok = False
        self._pr_status_cache = cache
        self._pr_status_cache_ok = ok

    def get_pr_status(self, branch: str) -> Optional[str]:
        """Получить статус PR по ТОЧНОЙ ветке (merged/closed/open/not-found).
        None — либо PR по этой ветке не найден, либо весь пакетный запрос
        не удался (сеть/gh недоступен) — вызывающий код (can_remove_worktree)
        обязан трактовать None как отказ, не как "можно удалять" (fail loud).
        """
        if not branch.startswith('agent/'):
            return None
        if self._pr_status_cache is None:
            self._load_pr_status_cache()
        if not self._pr_status_cache_ok:
            return None
        return self._pr_status_cache.get(branch)

    def can_remove_worktree(self, worktree_info: dict) -> Tuple[bool, str]:
        """
        Проверить, безопасно ли удалять worktree.
        Возвращает (can_remove, reason).
        """
        path = worktree_info['path']
        branch = self.branch_name(worktree_info.get('branch', 'unknown'))

        # Проверка 1: наличие незакоммиченных изменений — --force НЕ отменяет
        if self.check_dirty(path):
            return False, "Has uncommitted changes"

        # Проверка 2: наличие локальных коммитов, которых нет больше нигде —
        # --force НЕ отменяет
        if self.check_unpushed_commits(path):
            return False, "Has unpushed commits"

        # Проверка 3: статус PR — единственная проверка, пропускаемая
        # --force (debug-путь). Не "ветка есть на origin": оркестратор этого
        # репозитория не удаляет ветку после слияния (см. докстринг файла).
        # Не смогли определить статус (сеть/gh недоступен, PR не найден) —
        # отказ, а не молчаливое разрешение (fail loud, не silent-wrong).
        if not self.force:
            pr_status = self.get_pr_status(branch)
            if pr_status is None:
                return False, "Could not determine PR status (no PR found or gh unavailable) — keeping to be safe"
            if pr_status == 'open':
                return False, "Associated PR is still open"

        # Проверка 4: retention (дерево достаточно старое) — не зависит от --force
        age_hours = self.get_worktree_age_hours(path)
        if age_hours < self.retention_hours:
            return False, f"Too young (age: {age_hours:.1f}h < {self.retention_hours}h retention)"

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
            'unknown_pr_status': [],
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
                elif "Could not determine" in reason:
                    stats['unknown_pr_status'].append(branch)
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

        if stats['unknown_pr_status']:
            print(f"\nUnknown PR status (kept to be safe): {len(stats['unknown_pr_status'])}")
            for b in stats['unknown_pr_status'][:3]:
                print(f"  - {b}")
            if len(stats['unknown_pr_status']) > 3:
                print(f"  ... and {len(stats['unknown_pr_status']) - 3} more")

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

    # repo_root — от ТЕКУЩЕГО КАТАЛОГА ВЫЗОВА (os.getcwd()), не от
    # расположения самого файла скрипта (__file__). Разница критична: этот
    # скрипт вызывается из scripts/git/task-branch БЕЗ смены каталога — cwd
    # в проде это репозиторий агента, а в тестовом песочном прогоне
    # (scripts/git/test/task-branch.test.sh, `cd "$WORK/x-main" && ... bash
    # task-branch`) это ИЗОЛИРОВАННЫЙ временный git-репозиторий теста. Резолв
    # от __file__ проигнорировал бы песочницу и запустил бы настоящую уборку
    # по НАСТОЯЩЕМУ репозиторию разработчика при каждом прогоне теста
    # task-branch — обнаружено при подключении вызова (#891), не в проде.
    repo_root = os.getcwd()

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
