#!/usr/bin/env python3
"""Дешёвый механический ребейз конфликтных PR (issue #762).

Зачем отдельный модуль, не правка scheduler.py: `dispatch_conflict_rework`
(scheduler.py) уже расшивает конфликты, но ЕДИНСТВЕННЫМ путём — через
LLM-агента `worker.yml` (git rebase происходит внутри хода DSH). Живой
замер 2026-09-08: 25 конфликтных PR из 38 открытых, ни один не сливается —
при «воркер один на репозиторий, ровно один workflow_dispatch за пульс»
это 25 отдельных агентских проходов, каждый жжёт квоту дефицитного
LLM-провайдера, хотя сам `dispatch_conflict_rework` дословно говорит: «PR с
меткой conflict почти всегда просто отстал от main — git rebase
origin/main решает это без содержательного решения». Признака «дрейф или
содержательный конфликт» ДО попытки не существует (GitHub REST отдаёт
только mergeable_state, не конфликтующие ханки) — САМА попытка ребейза и
есть этот признак, а она дешёвая и не требует модели вовсе.

`scheduler.py` намеренно НЕ заводит локальный git-клон (см. докстринг
модуля и `openspec/changes/conflict-auto-rebase/proposal.md`, раздел «Что
вне рамок»: единственный источник состояния там — `gh api`). Этот модуль —
осознанное исключение из того принципа, поэтому живёт отдельно, не внутри
scheduler.py: он работает НАД РЕАЛЬНЫМ git-деревом (job с `actions/
checkout@v7`, `fetch-depth: 0`), которого у scheduler.py принципиально нет.

Разделение труда после этой правки:
  - `mechanical_rebase.py` (этот модуль) — берёт ВСЕ PR с меткой `conflict`
    за один проход, пытается `git rebase origin/main` МЕХАНИЧЕСКИ, без
    модели. Сошлось — пушит `--force-with-lease`; снятие метки `conflict`
    остаётся ЗА `mark_conflicts` (scheduler.py) — единственное место,
    которое меняет эту метку (не заводим вторую точку записи того же
    факта: `mark_conflicts` и так перепроверяет `mergeable_state` на
    следующем проходе, у него уже есть вся логика «„не знаю“ не значит
    „нет конфликта“», дублировать её здесь — второй источник истины).
  - `dispatch_conflict_rework` (scheduler.py, НЕ изменён) — по-прежнему
    зовёт агента, но теперь только для PR, которые механический проход НЕ
    смог свести: PR, у которого этот модуль снял метку `conflict` (руками
    mark_conflicts, следующим тактом), уже не попадает в выборку по метке
    `conflict` — агент на него не тратится.

Три исхода на PR (process_pull), различены по смыслу, а не по тексту
ошибки git (тот протухнет при смене формулировки — тот же принцип, что
WORKER_GIT_STEP_MARKER в scheduler.py):
  - "resolved"          — рёбейз сошёлся, ветка запушена `--force-with-lease`;
  - "conflict"           — рёбейз СТРУКТУРНО уткнулся в конфликт: после
                            неудачного `git rebase` существует каталог
                            `.git/rebase-merge` или `.git/rebase-apply` —
                            это признак самого git, не подстрочный матч
                            stderr. PR остаётся кандидатом агентского пути,
                            эта функция НЕ трогает assignee/замок/dispatch;
  - "ai-review-running"  — тот же тормоз, что update_branch (scheduler.py):
                            не двигаем head, пока по PR летит ai-review.yml
                            (review_labels.other_active_ai_review_runs —
                            переиспользован, вторая копия не заводится);
  - "infra-error: <текст>" — сбой НЕ через конфликт (сеть, права, ветка
                            удалена, force-with-lease отклонён гонкой) —
                            AGENTS.md, «fail loud, не гадать»: лечится
                            иначе, чем содержательный конфликт, поэтому
                            называется отдельной строкой в отчёте.

Обход очереди — от старейшего эпизода конфликта к новейшему (issue #588,
`sch.conflict_labeled_at` — переиспользован, вторая сортировка не
заводится): без этого свежие конфликты систематически обгоняли бы старые
голодающие PR тем же классом бага, что уже был найден и исправлен для
агентского пути.

Известный, документированный, самокорректирующийся край: между пушем этого
модуля и следующим пересчётом `mergeable_state` на стороне GitHub есть
асинхронное окно (см. `docs/research/21-github-actions.md` — GitHub не
гарантирует немедленный пересчёт). Если `orchestra`-job (mark_conflicts)
успеет спросить `mergeable_state` РАНЬШЕ, чем GitHub пересчитал его после
нашего пуша, метка `conflict` на мгновение вернётся — `.github/workflows/
orchestra.yml` сокращает это окно (`needs: conflict-mechanical-rebase`), не
устраняет полностью. Цена не бесплатна (спам-комментарий mark_conflicts
«PR конфликтует с main»), но следующий же проход этого модуля увидит
`git rebase` как no-op («Already up to date») и снова запушит/пропустит
без вреда — самокорректируется, отдельного тормоза не требует.

Запуск: python scripts/orchestra/mechanical_rebase.py (внутри job'а с
полным git-деревом и PAT-авторизацией git — см. .github/workflows/
conflict-mechanical-rebase.yml, СВОЙ workflow-файл, не job внутри
orchestra.yml — изоляция от pulse_guard.heartbeat_check, см. докстринг
самого workflow-файла и openspec/changes/mechanical-conflict-rebase/
design.md, «Развилка 2»).
Тесты: python -m pytest scripts/orchestra/test_mechanical_rebase.py -q
"""

import os
import subprocess
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # scheduler.py делает `from pulse_guard import …`

import scheduler as sch  # noqa: E402 — после sys.path выше, тот же приём, что тесты scheduler.py

review_labels = sch.review_labels  # уже загруженный scheduler'ом модуль — не грузим второй раз


class GitError(RuntimeError):
    """Сбой git-операции (сеть, права, отсутствующая ветка, force-with-lease
    отклонён гонкой) — ОТДЕЛЬНЫЙ класс от «структурный конфликт» (см.
    attempt_rebase: конфликт распознаётся по каталогу rebase-merge/-apply,
    не по этому исключению)."""


def run_git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result


def attempt_rebase(repo_dir: Path, head_ref: str) -> str:
    """Возвращает "resolved" (рёбейз сошёлся, working tree на перебазированной
    ветке — пуш ещё не сделан, это отдельный шаг push_rebased) или "conflict"
    (структурно уткнулись — рёбейз уже отменён `git rebase --abort`, working
    tree чист). Любой другой сбой — GitError."""
    run_git(["fetch", "origin", "main", head_ref], repo_dir)
    run_git(["checkout", "-B", head_ref, f"origin/{head_ref}"], repo_dir)
    result = run_git(["rebase", "origin/main"], repo_dir, check=False)
    if result.returncode == 0:
        return "resolved"
    # Структурный признак конфликта — каталог, который сам git заводит на
    # время паузы рёбейза, не текстовый матч stderr (тот протухнет при
    # смене формулировки git, тот же принцип, что WORKER_GIT_STEP_MARKER).
    if (repo_dir / ".git" / "rebase-merge").exists() or (repo_dir / ".git" / "rebase-apply").exists():
        run_git(["rebase", "--abort"], repo_dir)
        return "conflict"
    raise GitError(
        f"git rebase origin/main упал не через конфликт (rc={result.returncode}): "
        f"{(result.stderr or result.stdout).strip()}"
    )


def push_rebased(repo_dir: Path, head_ref: str) -> None:
    run_git(["push", "--force-with-lease", "origin", f"{head_ref}:{head_ref}"], repo_dir)


def conflict_queue(repo: str, pulls: list[dict]) -> list[dict]:
    """PR с меткой `conflict`, от старейшего эпизода к новейшему — та же
    дисциплина обхода, что `dispatch_conflict_rework` (issue #588):
    переиспользует `sch.conflict_labeled_at` (одно место правды), вторую
    сортировку не заводит."""
    conflict_pulls = [
        p for p in pulls
        if sch.CONFLICT_LABEL in {label["name"] for label in p["labels"]}
    ]
    conflict_pulls.sort(
        key=lambda p: sch.conflict_labeled_at(repo, p["number"]) or sch.parse_time(p["created_at"])
    )
    return conflict_pulls


def process_pull(repo: str, pull: dict, repo_dir: Path) -> str:
    """Один PR — см. докстринг модуля для полного разбора исходов."""
    number = pull["number"]
    head_ref = (pull.get("head") or {}).get("ref")
    if not head_ref:
        return "infra-error: PR без head.ref"
    try:
        running = review_labels.other_active_ai_review_runs(
            repo, number, exclude_run_id=None, gh_func=sch.gh)
        if running:
            return "ai-review-running"
        outcome = attempt_rebase(repo_dir, head_ref)
        if outcome == "conflict":
            return "conflict"
        push_rebased(repo_dir, head_ref)
        return "resolved"
    except RuntimeError as error:
        # GitError (git-слой) и обычный RuntimeError (sch.gh — сетевой/API
        # сбой) — один и тот же бюджет «не по конфликту» (AGENTS.md, «fail
        # loud, не гадать»): различать причину дальше здесь не нужно, важно
        # только не спутать её со структурным конфликтом.
        return f"infra-error: {error}"


def run(repo: str, repo_dir: Path) -> tuple[list[str], dict[int, str]]:
    pulls = sch.open_pulls(repo)
    queue = conflict_queue(repo, pulls)
    outcomes: dict[int, str] = {}
    resolved = conflicted = deferred = failed = 0
    lines: list[str] = []
    for pull in queue:
        number = pull["number"]
        outcome = process_pull(repo, pull, repo_dir)
        outcomes[number] = outcome
        if outcome == "resolved":
            resolved += 1
            lines.append(
                f"✅ PR #{number}: git rebase origin/main сошёлся механически, "
                "ветка обновлена и запушена (--force-with-lease); снятие метки "
                "`conflict` — за mark_conflicts следующим тактом"
            )
        elif outcome == "conflict":
            conflicted += 1
            lines.append(
                f"🔀 PR #{number}: рёбейз структурно уткнулся в конфликт — "
                "остаётся кандидатом агентского пути (dispatch_conflict_rework)"
            )
        elif outcome == "ai-review-running":
            deferred += 1
            lines.append(f"⏸️ PR #{number}: по нему летит ai-review.yml — head не двигаю в этом проходе")
        else:
            failed += 1
            lines.append(f"⚠️ PR #{number}: {outcome} — не конфликт, лечится отдельно")
    lines.insert(
        0,
        f"Механический ребейз конфликтов: {len(queue)} PR в очереди, "
        f"{resolved} сошлись механически, {conflicted} остаются конфликтом "
        f"(агентский путь), {deferred} отложены (ai-review), {failed} инфраструктурных сбоев",
    )
    return lines, outcomes


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    repo_dir = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
    lines, _outcomes = run(repo, repo_dir)
    sch.summary(lines)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::mechanical-rebase: {error}")
        sys.exit(1)
