#!/usr/bin/env python3
"""Планировщик оркестратора: следит за пулом задач и сливает проверенные PR.

Вызывается workflow'ом orchestra по расписанию (каждые 15 минут) и вручную.
Workflow держит concurrency-группу `orchestra`: два запуска планировщика
никогда не идут параллельно — это архитектурная сериализация слияний.

Обязанности:
  1. Просроченные назначения: задачу назначили, PR так и не появился за STALE_HOURS —
     назначение снимается, задача возвращается в пул (агент мог умереть посреди работы).
  2. Конфликты: открытые PR, которые больше не сливаются в main без ручного
     разрешения, получают метку `conflict` и комментарий со списком соперников.
  3. Очередь слияний: PR с зелёными проверками и чистым контрактом сливается.
     За один запуск — ровно один PR: после каждого слияния очередь пересчитывается
     следующим запуском, так конфликтующие слияния никогда не проходят одновременно.
  4. Пульс конвейера: если в пуле есть свободная задача и активного worker-рана
     нет — ровно один `workflow_dispatch` воркера (scripts/worker/task.sh,
     docs/agents/WORKER-PLAYBOOK.md). Best-effort: сбой диспатча не роняет
     планировщик.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

STALE_HOURS = 24
ONE_MERGE_PER_RUN = True
TASK_LABEL = "task"
CONFLICT_LABEL = "conflict"
MERGE_METHOD = "squash"


def gh(*args: str) -> dict | list | None:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:2])}: {result.stderr.strip()}")
    # Часть успешных вызовов (например, POST .../dispatches) отвечает 204 без
    # тела — отсутствие JSON это успех, а не ошибка разбора.
    return json.loads(result.stdout) if result.stdout.strip() else None


def summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    print(text)
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(text)


def parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def open_task_issues(repo: str) -> list[dict]:
    issues = gh(f"repos/{repo}/issues?state=open&labels={TASK_LABEL}&per_page=100")
    return [issue for issue in issues if "pull_request" not in issue]


def open_pulls(repo: str) -> list[dict]:
    return gh(f"repos/{repo}/pulls?state=open&per_page=100")


def pr_references_issue(pull: dict, issue_number: int) -> bool:
    body = pull.get("body") or ""
    return f"#{issue_number}" in body


def reap_stale(repo: str, now: datetime, pulls: list[dict]) -> list[str]:
    lines = []
    for issue in open_task_issues(repo):
        if not issue["assignees"]:
            continue
        number = issue["number"]
        if any(pr_references_issue(pull, number) for pull in pulls):
            continue
        timeline = gh(f"repos/{repo}/issues/{number}/timeline?per_page=100")
        assigned_at = [
            event["created_at"] for event in timeline
            if event.get("event") == "assigned"
        ]
        if not assigned_at:
            continue
        last = parse_time(max(assigned_at))
        if now - last < timedelta(hours=STALE_HOURS):
            continue
        who = ", ".join(a["login"] for a in issue["assignees"])
        gh(
            "-X", "DELETE", f"repos/{repo}/issues/{number}/assignees",
            "-f", f"assignees[]={who}",
        )
        gh(
            "-X", "POST", f"repos/{repo}/issues/{number}/comments",
            # body уходит ЗНАЧЕНИЕМ аргумента "-f body=…" (форма gh api):
            # keyword-аргумент gh() не принимает и роняет весь прогон (#124).
            "-f", "body=" + (
                f"Назначение снято оркестратором: за {STALE_HOURS} часов не появился PR, "
                f"а задача назначена {who}. Задача возвращена в пул — бери через assign."
            ),
        )
        lines.append(f"♻️ #{number} просрочена ({who}), возвращена в пул")
    return lines


def mark_conflicts(repo: str, pulls: list[dict]) -> list[str]:
    lines = []
    for pull in pulls:
        # mergeable_state живёт только на endpoint'е одиночного PR: в списке он
        # всегда отсутствует, и доверие ему — тихая потеря всех кандидатов.
        single = gh(f"repos/{repo}/pulls/{pull['number']}")
        if single.get("mergeable_state") != "dirty":
            continue
        labels = {label["name"] for label in pull["labels"]}
        if CONFLICT_LABEL in labels:
            continue
        gh("-X", "POST", f"repos/{repo}/issues/{pull['number']}/labels", "-f", f"labels[]={CONFLICT_LABEL}")
        rivals = ", ".join(f"#{other['number']}" for other in pulls if other["number"] != pull["number"]) or "нет"
        gh(
            "-X", "POST", f"repos/{repo}/issues/{pull['number']}/comments",
            "-f", "body=" + (
                f"PR конфликтует с main (открытые конкуренты: {rivals}). "
                "Перебазируй на свежий main и продолжай — оркестратор подхватит."
            ),
        )
        lines.append(f"⚠️ PR #{pull['number']} помечен `conflict`")
    return lines


def merge_queue(repo: str, pulls: list[dict]) -> list[str]:
    lines = []
    skipped = []
    for pull in pulls:
        if pull.get("draft"):
            skipped.append(f"#{pull['number']} — черновик")
            continue
        # mergeable_state живёт только на endpoint'е одиночного PR: в списке он
        # всегда отсутствует, и доверие ему — тихая потеря всех кандидатов.
        single = gh(f"repos/{repo}/pulls/{pull['number']}")
        state = single.get("mergeable_state")
        if state == "behind":
            # Ветка отстала от main — обновляем серверно. ВАЖНО: обновление через
            # GITHUB_TOKEN не зажигает проверки (защита от рекурсии) — бот-PR навсегда
            # зависает в blocked. Поэтому PAT, если задан.
            pat = os.environ.get("ORCHESTRA_PAT")
            if pat:
                subprocess.run(
                    ["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/{pull['number']}/update-branch",
                     "-H", f"Authorization: Bearer {pat}"],
                    capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"},
                    check=True,
                )
            else:
                gh("-X", "PUT", f"repos/{repo}/pulls/{pull['number']}/update-branch")
            skipped.append(f"#{pull['number']} — обновлена из main, проверки пойдут заново")
            continue
        if state not in ("clean", "unstable", "has_hooks"):
            skipped.append(f"#{pull['number']} — mergeable_state={state or 'не вычислен GitHub'}")
            continue
        checks = gh(f"repos/{repo}/commits/{pull['head']['sha']}/check-runs?per_page=100")
        runs = checks.get("check_runs", [])
        if not runs:
            skipped.append(f"#{pull['number']} — проверки ещё не заведены")
            continue
        bad = [run["name"] for run in runs if run["conclusion"] not in ("success", "skipped", "neutral")]
        if bad:
            skipped.append(f"#{pull['number']} — красные проверки: {', '.join(bad)}")
            continue
            continue
        labels = {label["name"] for label in pull["labels"]}
        if "review:ok" not in labels:
            skipped.append(f"#{pull['number']} — нет вердикта review:ok (ждёт ревью или доработку)")
            continue
        gh(
            "-X", "PUT", f"repos/{repo}/pulls/{pull['number']}/merge",
            "-f", f"merge_method={MERGE_METHOD}",
        )
        lines.append(f"✅ PR #{pull['number']} слит ({MERGE_METHOD})")
        if skipped:
            lines += [f"   (отложены: {item})" for item in skipped]
        lines += after_merge(repo, pull)
        return lines  # один за запуск: сериализация слияний
    if skipped:
        lines += [f"⏸️ {item}" for item in skipped]
    return lines


def after_merge(repo: str, pull: dict) -> list[str]:
    """Действия после слияния. Merge через GITHUB_TOKEN НЕ создаёт push-события
    (защита GitHub от рекурсии), поэтому за деплоем и закрытием задач следим явно."""
    lines = []
    number = pull["number"]
    files = gh(f"repos/{repo}/pulls/{number}/files?per_page=100")
    if any((f["filename"] or "").startswith("cf-worker/") for f in files):
        subprocess.run(
            ["gh", "workflow", "run", "deploy-worker.yml", "--ref", "main"],
            capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"},
            check=True,
        )
        lines.append("🚀 deploy-worker запущен (push от GITHUB_TOKEN триггеры не создаёт)")
    # Закрытие задачи — не здесь и не по ключевым словам: мерж доказывает PR,
    # а не готовность задачи, чей критерий часто живёт после мержа (деплой,
    # канарейка, E2E). Напоминаем исполнителю; закрытие — за ним, с уликами
    # (кейс #56/#57: Closes закрыл задачу до зелёной канарейки).
    task_refs = sorted({m.group(1) for m in re.finditer(r"#(\d+)", pull.get("body") or "")}, key=int)
    for task_number in task_refs:
        try:
            issue = gh(f"repos/{repo}/issues/{task_number}")
            if "pull_request" in issue or issue["state"] != "open":
                continue
            if "task" not in {label["name"] for label in issue["labels"]}:
                continue
            gh(
                "-X", "POST", f"repos/{repo}/issues/{task_number}/comments",
                "-f",
                "body=" + (
                    f"🔁 PR #{number} слит в main. Мерж — ещё не готовность: проведи "
                    "пост-мерж проверку (деплой/канарейка/E2E), приложи улики "
                    "и закрой задачу."
                ),
            )
            lines.append(f"🔁 #{task_number}: напоминание — закрыть после пост-мерж проверки")
        except RuntimeError as error:
            # один кривой реф не должен ронять остальные действия after_merge
            lines.append(f"⚠️ напоминание в #{task_number} не доставлено: {error}")
    return lines


def worker_runs_active(repo: str) -> bool:
    """Активный воркер = есть worker-ран в статусе in_progress или queued.
    Завершённые (в т.ч. упавшие) не считаются: упавший воркер при свободных
    задачах получит новый запуск — но пока задача назначена, пул свободных пуст
    и штурма не будет (возврат в пул только через stale-окно reap_stale)."""
    for status in ("in_progress", "queued"):
        payload = gh(
            f"repos/{repo}/actions/workflows/worker.yml/runs?status={status}&per_page=1"
        ) or {}
        if payload.get("workflow_runs"):
            return True
    return False


def dispatch_worker(repo: str, pool: list[dict]) -> list[str]:
    """Пульс конвейера: свободная задача есть, воркер простаивает → ровно один
    dispatch worker.yml за запуск оркестратора. Best-effort по построению:
    workflow ещё не на main (до мержа PR #89), права, сеть — любой сбой
    диспатча не роняет оркестратор, слияния важнее подряда воркеру."""
    lines = []
    free = [issue for issue in pool if not issue["assignees"]]
    if not free:
        return lines
    try:
        if worker_runs_active(repo):
            lines.append("👷 воркер уже работает — dispatch не нужен")
            return lines
        gh(
            "-X", "POST",
            f"repos/{repo}/actions/workflows/worker.yml/dispatches",
            "-f", "ref=main",
        )
        lines.append(
            f"👷 свободная задача #{free[0]['number']} — worker.yml запущен "
            "(воркер сам назначится и откроет PR)"
        )
    except RuntimeError as error:
        lines.append(f"⚠️ dispatch воркера не удался (не критично): {error}")
    return lines


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    lines = [f"## Отчёт оркестратора {now.isoformat(timespec='seconds')}", ""]

    pulls = open_pulls(repo)
    lines.append(f"Открытых PR: {len(pulls)}")

    stale_lines = reap_stale(repo, now, pulls)
    pulls = open_pulls(repo)  # состояние могло измениться
    conflict_lines = mark_conflicts(repo, pulls)
    merge_lines = merge_queue(repo, pulls)

    pool = open_task_issues(repo)
    free = sum(1 for issue in pool if not issue["assignees"])
    taken = len(pool) - free
    lines += ["", f"Пул задач: {free} свободно, {taken} в работе"]
    worker_lines = dispatch_worker(repo, pool)

    if stale_lines or conflict_lines or merge_lines or worker_lines:
        lines += ["", "### Действия",
                  *stale_lines, *conflict_lines, *merge_lines, *worker_lines]
    else:
        lines += ["", "Действий не требуется."]

    summary(lines)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::orchestra: {error}")
        sys.exit(1)
