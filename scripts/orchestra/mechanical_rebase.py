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

Четыре исхода на PR (process_pull), различены по смыслу, а не по тексту
ошибки git (тот протухнет при смене формулировки — тот же принцип, что
WORKER_GIT_STEP_MARKER в scheduler.py):
  - "resolved"          — рёбейз сошёлся, ветка запушена `--force-with-lease`;
  - "conflict"           — рёбейз СТРУКТУРНО уткнулся в конфликт: после
                            неудачного `git rebase` существует каталог
                            `.git/rebase-merge` или `.git/rebase-apply` И в
                            индексе есть НЕЗАВЕДЁННЫЕ пути (`git diff
                            --diff-filter=U`) — оба признака самого git, не
                            подстрочный матч stderr. Каталог паузы рёбейза
                            заводится и в этом случае, И тогда, когда патч
                            применился БЕЗ конфликта, но `git commit` внутри
                            рёбейза упал по другой причине (issue #764,
                            находка ревью, требование 1: отсутствующая git
                            identity на раннере даёт rc=128 «unable to
                            auto-detect email address» и ТОТ ЖЕ каталог паузы
                            без единого конфликтующего пути) — различаем по
                            наличию незаведённых путей, см. attempt_rebase.
                            PR остаётся кандидатом агентского пути, эта
                            функция НЕ трогает assignee/замок/dispatch;
  - "ai-review-running"  — тот же тормоз, что update_branch (scheduler.py):
                            не двигаем head, пока по PR летит ai-review.yml
                            (review_labels.other_active_ai_review_runs —
                            переиспользован, вторая копия не заводится);
  - "worker-running"     — issue #764, находка ревью, требование 2: не
                            двигаем head, пока по репозиторию активен
                            worker.yml (sch.worker_runs_active) — единственный
                            воркер может в этот момент пушить в ЛЮБУЮ
                            конфликтную ветку (dispatch_conflict_rework), а
                            метка `conflict` держится до следующего такта
                            mark_conflicts; без этого гейта механический
                            force-with-lease мог бы уехать НАД коммитами
                            агента и сжечь его единственную засчитанную
                            попытку (CONFLICT_REWORK_MAX_ATTEMPTS=1) вхолостую;
  - "infra-error: <текст>" — сбой НЕ через конфликт (сеть, права, ветка
                            удалена, force-with-lease отклонён гонкой,
                            отсутствующая git identity) — AGENTS.md, «fail
                            loud, не гадать»: лечится иначе, чем
                            содержательный конфликт, поэтому называется
                            отдельной строкой в отчёте.

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
нашего пуша, метка `conflict` на мгновение вернётся. НИКАКОЙ механизм это
окно не сокращает: `orchestra.yml` и `conflict-mechanical-rebase.yml` —
два независимых workflow-файла на одном 15-минутном кроне без взаимной
`needs:` (cross-workflow `needs` в GitHub Actions не существует вовсе, см.
design.md, «Развилка 2», «Цена решения» — находка ревью issue #764,
требование 3: более ранняя версия этого докстринга ошибочно утверждала
обратное). Цена не бесплатна (спам-комментарий mark_conflicts «PR
конфликтует с main»), но следующий же проход этого модуля увидит `git
rebase` как no-op («Already up to date») и снова запушит/пропустит без
вреда — самокорректируется, отдельного тормоза не требует.

Запуск: python scripts/orchestra/mechanical_rebase.py (внутри job'а с
полным git-деревом и PAT-авторизацией git — см. .github/workflows/
conflict-mechanical-rebase.yml, СВОЙ workflow-файл, не job внутри
orchestra.yml — изоляция от pulse_guard.heartbeat_check, см. докстринг
самого workflow-файла и openspec/changes/mechanical-conflict-rebase/
design.md, «Развилка 2»).

main() отказывается работать вне CI (issue #764, находка приёмки): требует
ОДНОВРЕМЕННО GITHUB_ACTIONS=true И GITHUB_RUN_ID (см. _require_ci_environment) —
ensure_clean_repo безусловно делает `git rebase --abort`/`reset --hard`/
`clean -fd` над repo_dir, в CI это безопасно (actions/checkout даёт свежее
дерево), но при случайном ручном запуске снесло бы незакоммиченные правки и
незавершённый рёбейз человека. Одного признака GITHUB_ACTIONS недостаточно:
.githooks/pre-commit использует ЭТУ ЖЕ переменную РОВНО НАОБОРОТ (её
присутствие ОТКЛЮЧАЕТ гвардию хука) и сам называет риск, что она утечёт в
локальное окружение (частый случай при работе с `gh`/`act`) — опираться на
неё одну означало бы наследовать тот же риск здесь.

Тесты: python -m pytest scripts/orchestra/test_mechanical_rebase.py -q
"""

import os
import shutil
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


def _rebase_paused(repo_dir: Path) -> bool:
    return (repo_dir / ".git" / "rebase-merge").exists() or (repo_dir / ".git" / "rebase-apply").exists()


def ensure_clean_repo(repo_dir: Path) -> None:
    """Защита от каскада (issue #764, находка ревью, требование 5): если
    `git rebase --abort` в attempt_rebase сам упадёт (сеть, файловая
    система) на ОДНОМ PR очереди, `.git/rebase-merge`/`-apply` остаётся, и
    СЛЕДУЮЩИЙ PR очереди получает `git checkout`/`git rebase`, падающие с
    «you need to resolve your current index first» — не потому, что у НЕГО
    есть конфликт, а потому, что предыдущая итерация не отмылась.
    Принудительно возвращает рабочее дерево в чистое состояние ПЕРЕД каждой
    попыткой: если abort сам не справился, каталог паузы удаляется вручную
    (структурный факт, не догадка) — решает класс, а не конкретный сбой."""
    if _rebase_paused(repo_dir):
        run_git(["rebase", "--abort"], repo_dir, check=False)
        for name in ("rebase-merge", "rebase-apply"):
            marker = repo_dir / ".git" / name
            if marker.exists():
                shutil.rmtree(marker, ignore_errors=True)
    run_git(["reset", "--hard"], repo_dir, check=False)
    run_git(["clean", "-fd"], repo_dir, check=False)


def attempt_rebase(repo_dir: Path, head_ref: str) -> str:
    """Возвращает "resolved" (рёбейз сошёлся, working tree на перебазированной
    ветке — пуш ещё не сделан, это отдельный шаг push_rebased) или "conflict"
    (структурно уткнулись — рёбейз уже отменён `git rebase --abort`, working
    tree чист). Любой другой сбой — GitError.

    Перед КАЖДОЙ попыткой — ensure_clean_repo (issue #764, требование 5):
    рабочее дерево гарантированно не несёт паузу рёбейза от предыдущего PR
    очереди."""
    ensure_clean_repo(repo_dir)
    run_git(["fetch", "origin", "main", head_ref], repo_dir)
    run_git(["checkout", "-B", head_ref, f"origin/{head_ref}"], repo_dir)
    result = run_git(["rebase", "origin/main"], repo_dir, check=False)
    if result.returncode == 0:
        return "resolved"
    # Каталог паузы рёбейза — структурный признак самого git, не текстовый
    # матч stderr (тот протухнет при смене формулировки, тот же принцип,
    # что WORKER_GIT_STEP_MARKER). НО каталог заводится в ДВУХ разных
    # случаях: (1) честный текстовый конфликт при cherry-pick патча, (2)
    # патч применился БЕЗ конфликта, но `git commit` внутри рёбейза упал по
    # другой причине — живой пример (issue #764, находка ревью, требование
    # 1): раннер без git identity, `git rebase` падает rc=128 «unable to
    # auto-detect email address», каталог паузы тот же самый. Различаем по
    # факту НЕЗАВЕДЁННЫХ путей в индексе (`git diff --diff-filter=U`) — это
    # тоже git-native структурный сигнал (unmerged paths), не подстрочный
    # матч stderr: настоящий конфликт всегда оставляет unmerged-запись,
    # сбой на этапе commit — никогда (мутационно проверено #764: rc=128 без
    # identity даёт `git diff --diff-filter=U` пустым).
    if _rebase_paused(repo_dir):
        unmerged = run_git(
            ["diff", "--name-only", "--diff-filter=U"], repo_dir, check=False
        ).stdout.strip()
        run_git(["rebase", "--abort"], repo_dir, check=False)
        if unmerged:
            return "conflict"
        raise GitError(
            "git rebase остановился (создан каталог паузы), но конфликтующих "
            f"путей в индексе нет (rc={result.returncode}) — это НЕ содержательный "
            "конфликт, а сбой на этапе commit (частая причина — не настроена git "
            f"identity в этом окружении): {(result.stderr or result.stdout).strip()}"
        )
    raise GitError(
        f"git rebase origin/main упал не через конфликт (rc={result.returncode}): "
        f"{(result.stderr or result.stdout).strip()}"
    )


def push_rebased(repo_dir: Path, head_ref: str) -> None:
    run_git(["push", "--force-with-lease", "origin", f"{head_ref}:{head_ref}"], repo_dir)


def _same_repo_agent_branch(repo: str, pull: dict) -> bool:
    """issue #764, находка ревью, требование 4: репозиторий публичный, PR из
    форка возможен — `head.ref` без проверки `head.repo.full_name` может
    называть ветку `origin` этого репозитория с СОВПАДАЮЩИМ именем, но
    принадлежащую чужому PR. Без этого фильтра механический путь ребейзил
    бы и `--force-with-lease`-пушил ЧУЖУЮ ветку `origin`, приписывая исход
    номеру PR из форка. Заодно исключаем ветки без префикса `agent/`
    (включая `main`) — тот же структурный признак, что использует
    task-branch/#356, не текстовый список исключений."""
    head = pull.get("head") or {}
    ref = head.get("ref") or ""
    head_repo_full_name = (head.get("repo") or {}).get("full_name")
    return head_repo_full_name == repo and ref.startswith("agent/")


def conflict_queue(repo: str, pulls: list[dict]) -> list[dict]:
    """PR с меткой `conflict`, от старейшего эпизода к новейшему — та же
    дисциплина обхода, что `dispatch_conflict_rework` (issue #588):
    переиспользует `sch.conflict_labeled_at` (одно место правды), вторую
    сортировку не заводит. Фильтрует чужие/форкнутые ветки — см.
    _same_repo_agent_branch."""
    conflict_pulls = [
        p for p in pulls
        if sch.CONFLICT_LABEL in {label["name"] for label in p["labels"]}
        and _same_repo_agent_branch(repo, p)
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
        # issue #764, находка ревью, требование 2: взаимное исключение с
        # агентским путём. `dispatch_conflict_rework` (scheduler.py) снимает
        # assignee/замок и диспатчит worker.yml адресно на ОДИН PR, но метка
        # `conflict` держится до следующего такта mark_conflicts, и оба
        # такта (orchestra.yml, conflict-mechanical-rebase.yml) — на одном
        # 15-минутном кроне без взаимной `needs:` (design.md, «Развилка 2»).
        # Без гейта механический force-with-lease мог бы уехать НАД
        # коммитами уже работающего агента: тот получил бы отклонённый push
        # после честной работы, а conflict_rework_attempts уже засчитал бы
        # попытку по WORKER_GIT_STEP_MARKER — эскалация владельцу соврала бы
        # «конфликт не сошёлся» про попытку, которую механический путь сам и
        # уничтожил (тот же класс, что уже закрывали #588/#597 дорогой
        # ценой). Гейт repo-wide (sch.worker_runs_active(repo)), не per-PR:
        # воркер и так единственный активный на весь репозиторий («ровно
        # один workflow_dispatch за пульс», dispatch_worker/
        # dispatch_conflict_rework), точная привязка «прогон именно по
        # задаче ЭТОГО PR» потребовала бы для каждого PR очереди ещё один
        # запрос к API (task_ref.resolve_pr_task + атрибуция прогона) — цена
        # без выигрыша, раз пересечение и так исключено на уровне «воркер
        # один», тот же уровень точности, что уже применяет
        # dispatch_conflict_rework (worker_runs_active(repo), не per-task,
        # scheduler.py:778,855).
        if sch.worker_runs_active(repo):
            return "worker-running"
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
    resolved = conflicted = ai_deferred = worker_busy = failed = 0
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
            ai_deferred += 1
            lines.append(f"⏸️ PR #{number}: по нему летит ai-review.yml — head не двигаю в этом проходе")
        elif outcome == "worker-running":
            worker_busy += 1
            lines.append(
                f"⏸️ PR #{number}: воркер (worker.yml) активен по репозиторию — "
                "head не двигаю в этом проходе, чтобы не сжечь агентскую попытку (#764)"
            )
        else:
            failed += 1
            lines.append(f"⚠️ PR #{number}: {outcome} — не конфликт, лечится отдельно")
    lines.insert(
        0,
        f"Механический ребейз конфликтов: {len(queue)} PR в очереди, "
        f"{resolved} сошлись механически, {conflicted} остаются конфликтом "
        f"(агентский путь), {ai_deferred} отложены (ai-review), {worker_busy} "
        f"отложены (воркер занят), {failed} инфраструктурных сбоев",
    )
    return lines, outcomes


def _require_ci_environment() -> None:
    """Отказ вне CI (issue #764, находка приёмки): ensure_clean_repo
    безусловно делает `git rebase --abort`, `git reset --hard` и
    `git clean -fd` над repo_dir — в job'е с actions/checkout это безопасно
    (дерево всегда свежее), но main() берёт repo_dir из GITHUB_WORKSPACE ИЛИ
    Path.cwd(), а докстринг модуля приглашает запускать его вручную. Без
    этой проверки случайный ручной запуск (например, локальная отладка с
    экспортированным GITHUB_REPOSITORY) снёс бы незакоммиченные правки
    человека и его незавершённый рёбейз с уже решёнными конфликтами —
    приёмка воспроизвела это живьём.

    Одного GITHUB_ACTIONS недостаточно: .githooks/pre-commit опирается на ТУ
    ЖЕ переменную РОВНО НАОБОРОТ (её наличие ОТКЛЮЧАЕТ гвардию хука) и сам
    называет риск, что GITHUB_ACTIONS=true утечёт в локальное окружение
    (например, при работе с `gh`/`act`) — доверять одному этому признаку
    здесь означало бы наследовать тот же риск. GITHUB_RUN_ID — второй,
    независимый признак: его выставляет только сам раннер Actions на старте
    job'а, руками его никто не экспортирует."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("GITHUB_RUN_ID"):
        raise RuntimeError(
            "mechanical_rebase.main() отказывается запускаться вне GitHub Actions "
            "(нужны ОБА признака: GITHUB_ACTIONS=true и GITHUB_RUN_ID) — "
            "ensure_clean_repo внутри этого модуля безусловно выполняет над "
            "repo_dir `git rebase --abort`, `git reset --hard` и `git clean -fd`, "
            "что снесёт незакоммиченные правки и незавершённый рёбейз в текущем "
            "рабочем дереве. Запускай только через .github/workflows/"
            "conflict-mechanical-rebase.yml."
        )


def main() -> int:
    _require_ci_environment()
    repo = os.environ["GITHUB_REPOSITORY"]
    repo_dir = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
    lines, outcomes = run(repo, repo_dir)
    sch.summary(lines)
    # issue #764, находка ревью, требование 5: раньше эта функция ВСЕГДА
    # возвращала 0 — 25 из 25 "infra-error" (например, все PR очереди упёрлись
    # в один и тот же сбой git identity, находка 1) давали зелёный job, а
    # `failure_watch` (pulse_guard.py) читает только conclusion=="failure"/
    # "timed_out" запуска — запись в WATCHED_WORKFLOWS страховала бы падение
    # ПРОЦЕССА, но не «прошёл и не сделал ничего». Полный отказ очереди —
    # ненулевой исход, чтобы это стало видно тем же механизмом.
    if outcomes and failed_count(outcomes) == len(outcomes):
        print(
            "::error::mechanical-rebase: все PR очереди ("
            f"{len(outcomes)}) завершились инфраструктурной ошибкой — 0 обработано",
        )
        return 1
    return 0


def failed_count(outcomes: dict[int, str]) -> int:
    return sum(1 for outcome in outcomes.values() if outcome.startswith("infra-error"))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::mechanical-rebase: {error}")
        sys.exit(1)
