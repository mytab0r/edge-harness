#!/usr/bin/env python3
"""Отложенная уборка веток `agent/*` на origin после слияния/закрытия PR (#940).

Почему не `delete_branch_on_merge` (настройка репозитория): PR #937 явно
опирается на то, что `delete_branch_on_merge=false` — гейт
`dsh_worker_run_is_success` (`scripts/lib/dsh-ci.sh`) сравнивает
`git ls-remote origin refs/heads/<branch>` ДО и ПОСЛЕ прогона воркера в
пределах ОДНОГО job'а, чтобы поймать пуш, обошедший файловую песочницу
(линкованный worktree). Мгновенное удаление ветки в момент слияния убрало бы
этот сигнал ровно тогда, когда он нужен. Включать флаг на репозитории нельзя
(проверено `gh api repos/{repo} --jq .delete_branch_on_merge` = false, менять
не будем) — вместо этого ветка удаляется здесь, ОТДЕЛЬНЫМ периодическим
прогоном, не раньше `RETENTION_HOURS` часов после слияния/закрытия PR.

Retention story: `RETENTION_HOURS = 24` — с большим запасом (в 4 раза) над
максимальным временем job'а GitHub Actions на этом плане (6 часов, см.
AGENTS.md, docs/research/21-github-actions.md). Ко времени, когда эта уборка
может тронуть ветку, воркер, чей гейт читает её `ls-remote`-снимок, уже
десятки раз успел завершиться — снимок «после» этого job'а физически не
может совпасть по времени с отложенным удалением. Не тот же час, что
`scripts/git/worktree-cleanup.py` держит для ЛОКАЛЬНЫХ рабочих деревьев
(1 час) — там другая опасность (диск), не тот гейт, второе число не
дублирует первое, а отвечает на другой вопрос.

Источник дедлайна — не факт существования ветки (в этом репозитории
оркестратор НЕ удаляет ветку при слиянии, см. `worktree-cleanup.py`), а
статус PR: `merged`/`closed` без ни одного `open` PR на ту же ветку
(агрегация с тем же приоритетом open > merged/closed, что уже применяет
`worktree-cleanup.py` — ветка `agent/<N>-<slug>` в этом репозитории
переиспользуется при перезапуске задачи после закрытого PR).

Безопасность:
  - Трогает только ветки `agent/*` (защита от опечатки/чужой ветки).
  - Никогда не трогает ветку с открытым PR.
  - Никогда не трогает ветку моложе RETENTION_HOURS с момента последнего
    merged/closed события среди её PR.
  - Не смогли определить статус PR (сеть/`gh` недоступен, PR не найден) —
    ветка НЕ удаляется (fail loud, не молчаливое разрешение).
  - Не подтверждено этим скриптом (честно, не гадаем): что ветка не получила
    новых пушей ПОСЛЕ слияния (squash-merge не оставляет голову ветки
    предком main — сверить нечем без дополнительного сетевого вызова по
    каждой ветке). Защита — только конвенция репозитория: ветка `agent/N-*`
    привязана к ОДНОЙ задаче, закрытая задача не переоткрывается
    (AGENTS.md), новая работа заводит новую ветку — постмерж-пуш в старую
    ветку штатным потоком не происходит.

Запуск: `python scripts/git/merged-branch-cleanup.py [--dry-run] [--limit N]`
"""

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

RETENTION_HOURS = 24.0

# Тот же приоритет, что worktree-cleanup.py: `open` побеждает любой другой
# статус на ту же ветку (переиспользование ветки при перезапуске задачи).
_PR_STATUS_PRIORITY = {"OPEN": 2, "MERGED": 1, "CLOSED": 1}


def run_cmd(args: list[str]) -> tuple[str, int]:
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip(), result.returncode


def fetch_pr_records(limit: int = 2000) -> Optional[list[dict]]:
    """`gh pr list --state all` пакетно (одна ветка репозитория — одна ставка
    HTTP, не по одному вызову на кандидата, тот же приём, что worktree-cleanup.py).
    None — вызов не удался (сеть/gh), вызывающий обязан трактовать это как
    отказ, не как «кандидатов нет». `number` — не нужен `branch_deletion_candidates`
    самому, но нужен `scripts/git/local-refs-cleanup.py` (кэш-ссылки
    `refs/remotes/pr/<N>` по номеру, не по ветке) — общий фетч, второй сетевой
    вызов на тот же список не заводим."""
    out, rc = run_cmd([
        "gh", "pr", "list", "--state", "all",
        "--json", "number,headRefName,state,mergedAt,closedAt",
        "--limit", str(limit),
    ])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return None


def fetch_live_agent_branches() -> Optional[list[str]]:
    """`git ls-remote --heads origin` — какие ветки `agent/*` реально живы на
    origin прямо сейчас. None — вызов не удался."""
    out, rc = run_cmd(["git", "ls-remote", "--heads", "origin", "refs/heads/agent/*"])
    if rc != 0:
        return None
    branches = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        _, ref = line.split("\t", 1)
        prefix = "refs/heads/"
        if ref.startswith(prefix):
            branches.append(ref[len(prefix):])
    return branches


def branch_deletion_candidates(
    live_branches: list[str],
    pr_records: list[dict],
    now: datetime,
    retention_hours: float = RETENTION_HOURS,
) -> tuple[list[str], dict[str, str]]:
    """Чистая функция: (ветки к удалению, {ветка: причина оставить}).

    Условия удаления (ВСЕ обязаны выполниться, как у worktree-cleanup.py):
      1. Ветка начинается с `agent/` (защита от чужой ветки).
      2. Есть хотя бы одна запись PR по этой ветке (нет записи — не знаем
         статуса, оставляем).
      3. Ни одна запись по ветке не в статусе OPEN.
      4. Самое позднее merged/closed-событие среди записей старше
         retention_hours.
    """
    by_branch: dict[str, list[dict]] = {}
    for rec in pr_records:
        ref = rec.get("headRefName")
        if not ref:
            continue
        by_branch.setdefault(ref, []).append(rec)

    deletable: list[str] = []
    kept: dict[str, str] = {}

    for branch in live_branches:
        if not branch.startswith("agent/"):
            kept[branch] = "не agent/* ветка — не трогаем"
            continue
        records = by_branch.get(branch)
        if not records:
            kept[branch] = "нет записи PR по этой ветке — не знаем статуса"
            continue
        if any(r.get("state") == "OPEN" for r in records):
            kept[branch] = "есть открытый PR на эту ветку"
            continue
        times = []
        for r in records:
            raw = r.get("mergedAt") or r.get("closedAt")
            if raw:
                times.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        if not times:
            kept[branch] = "PR есть, но без mergedAt/closedAt — не знаем возраста"
            continue
        latest = max(times)
        age_hours = (now - latest).total_seconds() / 3600
        if age_hours < retention_hours:
            kept[branch] = f"моложе retention ({age_hours:.1f}ч < {retention_hours}ч)"
            continue
        deletable.append(branch)

    return deletable, kept


def delete_branch(branch: str) -> bool:
    """`git push origin --delete <branch>` — тот же путь, которым ветка
    создавалась (push), не REST-обход в обратную сторону. Не тот же класс
    отказа, что issue #882 (health-snapshot push молча проваливался без
    `gh auth setup-git`): там шаг клонирует РЕПОЗИТОРИЙ ЗАНОВО в
    `$RUNNER_TEMP` (свежий клон без учётных данных); этот скрипт работает в
    каталоге, который `actions/checkout@v7` УЖЕ настроил (credential helper
    на `GITHUB_TOKEN` через `http.<url>.extraheader`, персистентный по
    умолчанию) — второй `git push origin` от того же checkout'а те же
    креды уже наследует, отдельная авторизация не нужна."""
    _, rc = run_cmd(["git", "push", "origin", "--delete", branch])
    return rc == 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="только показать кандидатов")
    parser.add_argument("--limit", type=int, default=50, help="максимум удалений за прогон")
    args = parser.parse_args()

    pr_records = fetch_pr_records()
    if pr_records is None:
        print("🚨 gh pr list не удался — уборка веток пропущена (fail loud, не гадаем)", file=sys.stderr)
        return 1

    live_branches = fetch_live_agent_branches()
    if live_branches is None:
        print("🚨 git ls-remote не удался — уборка веток пропущена (fail loud, не гадаем)", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    deletable, kept = branch_deletion_candidates(live_branches, pr_records, now)

    print(f"Живых веток agent/*: {len(live_branches)}")
    print(f"Кандидатов на удаление (merged/closed ≥ {RETENTION_HOURS}ч): {len(deletable)}")
    print(f"Оставлено: {len(kept)}")

    to_process = deletable[: args.limit]
    removed = 0
    errors = []
    for branch in to_process:
        if args.dry_run:
            print(f"DRY-RUN: удалил бы {branch}")
            removed += 1
            continue
        if delete_branch(branch):
            print(f"Удалена: {branch}")
            removed += 1
        else:
            print(f"ОШИБКА: не удалось удалить {branch}", file=sys.stderr)
            errors.append(branch)

    print(f"Итого удалено: {removed}/{len(to_process)} (кандидатов всего {len(deletable)})")
    if errors:
        print(f"Ошибок: {len(errors)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
