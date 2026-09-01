#!/usr/bin/env python3
"""Атомарная аренда задачи (#121): единственный способ начать работу над задачей.

Носитель замка — серверный git-ref `refs/locks/task-<N>`. Атомарность даёт сам
GitHub: создание ref'а — серверная операция, повторный claim на существующий
ref отклоняется кодом 422 (гонка двух claim'ов выигрывает ровно один). Замок
указывает на коммит, созданный в момент claim'а (date коммита = время аренды) —
на нём стоит TTL. Освобождение — удаление ref'а.

Порядок claim: сначала замок (защита), потом назначение assignee и комментарий
в задачу (видимость — НЕ защита: все агенты работают под одним логином).

Отказ claim'а — нормальный исход («задача занята»), а не поломка: вызывающий
код обязан завершиться зелёным no-op. Инфраструктурный сбой (сеть, права) —
наоборот, громкий RuntimeError: «инструмент сломан» и «задача занята» лечатся
по-разному (fail loud).

Каналы, которым здесь жить после #119 (worker task.sh free_task/manual,
hands/runner-bridge): один вызов CLI — `claim_task.py claim <N>`; контракт
вызова и точки врезки зафиксированы в #121. До #119 эти файлы не трогаются.

Обёртка gh() дублирует таковую в scripts/orchestra (pulse_guard.py, и там же
обоснование): каждый скрипт — самостоятельная точка входа без пакетной
системы. Гвардия класса #124 (keyword body= в gh-вызове) распространена
шагом repo-ci и на scripts/lib.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# Один порог с оркестраторским окном просрочки назначений (scheduler.STALE_HOURS):
# замок и назначение протухают в одном такте, задача возвращается в пул целиком.
LOCK_TTL_HOURS = 24

LOCK_REF_PREFIX = "refs/locks/task-"

# Коды выхода CLI: 0 — claim получен / release выполнен; 1 — отказ («занята»,
# зелёный no-op у вызывающего); 2 — инструмент сломался (громко).
EXIT_OK = 0
EXIT_BUSY = 1
EXIT_ERROR = 2


def gh(*args: str) -> dict | list | None:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise GhError(result.stderr.strip(), gh_status(result.stderr))
    return json.loads(result.stdout) if result.stdout.strip() else None


class GhError(RuntimeError):
    """Сбой вызова gh с HTTP-статусом, если тот удалось разобрать."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def gh_status(stderr: str) -> int | None:
    match = re.search(r"HTTP (\d{3})", stderr or "")
    return int(match.group(1)) if match else None


def lock_ref(task: int) -> str:
    if not isinstance(task, int) or isinstance(task, bool) or task <= 0:
        raise ValueError(f"номер задачи должен быть целым > 0: {task!r}")
    return f"{LOCK_REF_PREFIX}{task}"


class ClaimResult:
    def __init__(self, claimed: bool, task: int, detail: str):
        self.claimed = claimed
        self.task = task
        self.detail = detail

    def __repr__(self):
        return f"ClaimResult(claimed={self.claimed}, task={self.task}, detail={self.detail!r})"


def task_of_ref(ref: str) -> int | None:
    if not ref.startswith(LOCK_REF_PREFIX):
        return None
    suffix = ref[len(LOCK_REF_PREFIX):]
    return int(suffix) if suffix.isdigit() else None


# ── Чистые решения: TTL по дате коммита замка ────────────────────────────────────


def lock_age_hours(commit_date: str | datetime, now: datetime) -> float:
    if isinstance(commit_date, str):
        commit_date = datetime.fromisoformat(commit_date.replace("Z", "+00:00"))
    return (now - commit_date).total_seconds() / 3600


def is_stale(commit_date: str | datetime, now: datetime,
             ttl_hours: float = LOCK_TTL_HOURS) -> bool:
    return lock_age_hours(commit_date, now) > ttl_hours


# ── Claim / release ──────────────────────────────────────────────────────────────


def claim(repo: str, task: int, actor: str, now: datetime | None = None) -> ClaimResult:
    """Атомарный захват задачи. Успех у ровно одного претендента; проигравший
    получает ClaimResult(claimed=False) и обязан закончиться зелёным no-op.
    Проигравший оставляет осиротевший коммит (ref на него не создан) — мусор
    без ссылки, безвреден; атомарность живёт в создании ref'а, раньше её
    получить из git нечем."""
    now = now or datetime.now(timezone.utc)
    ref = lock_ref(task)
    # Замок указывает на собственный коммит: его date — время аренды (TTL).
    base = gh(f"repos/{repo}/commits/main")
    commit = gh(
        "-X", "POST", f"repos/{repo}/git/commits",
        "-f", f"message=lock: task #{task} claimed by {actor} at "
              f"{now.isoformat(timespec='seconds')} (ttl {LOCK_TTL_HOURS}h)",
        "-f", f"tree={base['commit']['tree']['sha']}",
        "-f", f"parents[]={base['sha']}",
    )
    try:
        gh("-X", "POST", f"repos/{repo}/git/refs",
           "-f", f"ref={ref}", "-f", f"sha={commit['sha']}")
    except GhError as error:
        if error.status == 422:  # «Reference already exists» — замок уже стоит
            return ClaimResult(claimed=False, task=task,
                               detail=f"задача #{task} уже занята (замок {ref})")
        raise
    # Замок стоит — мы владельцы. Видимость: назначение и след в задаче.
    # Сбой видимости замок не отменяет (откат хуже отсутствия комментария),
    # но и не глотается: warning уходит в лог job'а.
    _visibility(repo, task, actor, f"🔒 Аренда задачи: `{actor}` держит замок `{ref}` "
                                  f"(TTL {LOCK_TTL_HOURS} ч по коммиту замка).")
    return ClaimResult(claimed=True, task=task, detail=f"замок {ref} установлен")


def _visibility(repo: str, task: int, actor: str, text: str) -> None:
    for call in (
        lambda: gh("-X", "POST", f"repos/{repo}/issues/{task}/assignees",
                   "-f", f"assignees[]={actor}"),
        lambda: gh("-X", "POST", f"repos/{repo}/issues/{task}/comments",
                   "-f", f"body={text}"),
    ):
        try:
            call()
        except RuntimeError as error:
            print(f"::warning::видимость аренды #{task} неполная: {error}", file=sys.stderr)


def release(repo: str, task: int) -> str:
    """Снять замок. Идемпотентно: отсутствующий замок — не ошибка.

    GitHub REST на DELETE несуществующего ref отвечает НЕ одним кодом:
    исторически документирован 404, но реально (проверено прогоном orchestra
    33562818220) отдаёт 422 с телом "Reference does not exist". Оба кода —
    один и тот же класс «рефа нет»; различать их нужно по семантике сообщения,
    а не по одному зашитому статусу, иначе withstand real-world ответа не будет.
    403/500 и прочие статусы — настоящая поломка, пробрасываются дальше."""
    try:
        gh("-X", "DELETE", f"repos/{repo}/git/refs/locks/task-{task}")
    except GhError as error:
        if _ref_missing(error):
            return f"замок task-{task} отсутствовал (уже свободна)"
        raise
    return f"замок refs/locks/task-{task} снят"


def _ref_missing(error: "GhError") -> bool:
    """Рефа нет: GitHub видели и с 404, и с 422 (Reference does not exist) —
    оба варианта отвечают за «рефа нет», не за поломку инструмента."""
    if error.status == 404:
        return True
    return error.status == 422 and "does not exist" in str(error).lower()


# ── Сборщик протухших замков (вызывает scheduler) ────────────────────────────────


def list_locks(repo: str) -> list[dict]:
    """Все замки: [{ref, sha, task}] (GET git/matching-refs/locks/)."""
    refs = gh("repos/{0}/git/matching-refs/locks/".format(repo)) or []
    locks = []
    for ref in refs:
        task = task_of_ref(ref["ref"])
        if task is None:  # чужой ref под locks/ — не наш, не трогаем
            continue
        locks.append({"ref": ref["ref"], "sha": ref["object"]["sha"], "task": task})
    return locks


def lock_commit_date(repo: str, sha: str) -> datetime:
    commit = gh(f"repos/{repo}/commits/{sha}")
    return datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00"))


def collect_stale(repo: str, now: datetime, ttl_hours: float = LOCK_TTL_HOURS) -> list[str]:
    """Снять протухшие замки и оставить след в задаче. Назначение при этом не
    трогается: возврат assignee в пул — работа механизма просроченных
    назначений (reap_stale, тот же порог 24 ч) — второе место правды для того
    же класса создавать запрещено. Замок и назначение протухают в одном такте."""
    lines = []
    for lock in list_locks(repo):
        date = lock_commit_date(repo, lock["sha"])
        if not is_stale(date, now, ttl_hours):
            lines.append(f"🔒 замок task-{lock['task']} жив "
                         f"({lock_age_hours(date, now):.1f} ч из {ttl_hours})")
            continue
        try:
            gh("-X", "DELETE", f"repos/{repo}/git/refs/locks/task-{lock['task']}")
        except GhError as error:
            lines.append(f"⚠️ замок task-{lock['task']} не снят: {error}")
            continue
        try:
            gh("-X", "POST", f"repos/{repo}/issues/{lock['task']}/comments",
               "-f", "body=" + (
                   f"🔓 Протухший замок снят оркестратором: аренда держалась дольше "
                   f"{ttl_hours} ч (коммит замка от {date.isoformat(timespec='seconds')}). "
                   "Замок убран; назначение снимает механизм просроченных назначений "
                   "с тем же порогом. Задача свободна — бери через claim."
               ))
        except RuntimeError as error:
            lines.append(f"⚠️ след в #{lock['task']} не оставлен: {error}")
        lines.append(f"♻️ замок task-{lock['task']} протух "
                     f"({lock_age_hours(date, now):.1f} ч) — снят, задача в пул")
    return lines


def release_merged(repo: str, task_numbers: list[int]) -> list[str]:
    """Release после слияния PR: снять замки задач, упомянутых в теле PR.
    Замка может не быть (канал без аренды) — идемпотентный release молчит."""
    return [f"🔓 {release(repo, task)}" for task in task_numbers]


def current_actor() -> str:
    return (os.environ.get("CLAIM_ACTOR") or os.environ.get("GITHUB_ACTOR")
            or os.environ.get("WORKER_LOGIN") or "unknown")


# ── CLI: один вызов для каналов worker/hands (см. контракт в #121) ───────────────


def main(argv: list[str]) -> int:
    usage = "использование: claim_task.py claim <N> | release <N> | status"
    if len(argv) < 2:
        print(f"::error::{usage}", file=sys.stderr)
        return EXIT_ERROR
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        print("::error::GITHUB_REPOSITORY не задан", file=sys.stderr)
        return EXIT_ERROR
    command = argv[1]
    try:
        if command in ("claim", "release") and len(argv) == 3 and argv[2].isdigit():
            task = int(argv[2])
            if command == "claim":
                result = claim(repo, task, current_actor())
                print(result.detail)
                return EXIT_OK if result.claimed else EXIT_BUSY
            print(release(repo, task))
            return EXIT_OK
        if command == "status":
            now = datetime.now(timezone.utc)
            for lock in list_locks(repo):
                date = lock_commit_date(repo, lock["sha"])
                age = lock_age_hours(date, now)
                state = "ПРОТУХ" if is_stale(date, now) else "жив"
                print(f"{lock['ref']}  {lock['sha'][:12]}  {age:.1f} ч  [{state}]")
            return EXIT_OK
    except RuntimeError as error:
        print(f"::error::claim_task: {error}", file=sys.stderr)
        return EXIT_ERROR
    print(f"::error::{usage}", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv))
