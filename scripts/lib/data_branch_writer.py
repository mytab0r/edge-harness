#!/usr/bin/env python3
"""Одно место правды на «append строку/файл + push с ретраем на data-ветку»
(issue #882).

До этого файла приём был написан ДВАЖДЫ, независимо, с одним и тем же
дефектом: `scripts/measure/pipeline_health.py::append_snapshot_and_push` и
`scripts/measure/dispatch_tail.py::append_and_push`. Докстринг первого прямо
называл второй образцом — тем не менее обе копии несли одну и ту же дыру:
после неудачного `push` восстановление (`git fetch`/`git checkout -B`) шло с
`check=False` — отказ проглатывался молча. Живой инцидент (#882): свежий клон
в `$RUNNER_TEMP` не наследует креды `actions/checkout`, а
`gh auth setup-git` в orchestra.yml не вызывался — `push` падал 403, `fetch`/
`checkout` тоже (ветки `data/pipeline-health` на origin ещё не существовало —
`checkout -B <br> origin/<br>` не может отработать, когда `origin/<br>` нет),
но так как оба шли с `check=False`, следующая итерация цикла как ни в чём не
бывало перечитывала СВОЙ собственный, ещё не запушенный локальный коммит и
принимала его за «кто-то другой уже записал снимок за сегодня» — молчание
конвейера маскировалось под здоровую дедупликацию гонки.

Различение гонки и отказа авторизации (`classify_push_failure`) — по STDERR
самого `git push`:
  - явные маркеры прав/аутентификации (403, permission, could not read
    username/password, invalid username or password, protected branch, …)
    → `"auth"` — красный шаг НЕМЕДЛЕННО, retry/фетч не имеет смысла (тот же
    токен даст тот же отказ на следующей попытке);
  - явные маркеры неудачного fast-forward (`[rejected]`,
    `non-fast-forward`, `fetch first`, `failed to push some refs`, …) без
    маркеров прав → `"race"` — штатный повтор: кто-то другой успел раньше;
  - ни один маркер не совпал → `"unknown"` — консервативно НЕ считается
    гонкой (тормоз без объявленного газа опаснее лишнего красного шага,
    AGENTS.md).

Штатный повтор (`"race"`) обязан подтвердить «чужая запись уже есть» ФАКТОМ
С СЕРВЕРА, а не содержимым локального рабочего дерева: `fetch`/`checkout`
восстановления здесь идут БЕЗ `check=False` (громко) — тихий откат и был
причиной инцидента. Дополнительная защита: если после гонки ветка
`origin/<br>` всё ещё не существует, конкурента, который мог её создать,
физически нет — это переквалифицируется в громкий отказ (не гонка), а не
тихое продолжение цикла.

Использование:
    from data_branch_writer import git, clone_data_branch, append_and_push,
        classify_push_failure
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

# Библиотечный модуль, не самостоятельная точка входа (нет блока запуска
# скрипта, никем не вызывается как файл-скрипт) — console_utf8 подключают
# вызывающие точки входа
# (pipeline_health.py/dispatch_tail.py), тот же приём, что у review_labels.py/
# pool_issue.py (см. scripts/lib/test_console_utf8_guard.py — bootstrap
# обязателен только для точек входа, не для их библиотечных зависимостей).

# ── Классификация отказа push (пусто-сетевая, тестируется на синтетическом stderr) ──

# Порядок проверки важен: реальный 403 часто несёт ОБА класса маркеров разом
# ("remote: Permission to owner/repo.git denied to user." +
# "fatal: unable to access ... 403" + generic "failed to push some refs") —
# маркеры прав проверяются первыми, иначе generic-хвост сообщения об ошибке
# классифицировал бы авторизационный отказ как безобидную гонку.
AUTH_FAILURE_MARKERS = (
    "403",
    "permission",
    "authentication failed",
    "could not read username",
    "could not read password",
    "invalid username or password",
    "access denied",
    "protected branch",
    "requires authentication",
)

RACE_MARKERS = (
    "[rejected]",
    "non-fast-forward",
    "fetch first",
    "failed to push some refs",
    "stale info",
    "tip of your current branch is behind",
)


def classify_push_failure(stderr: str) -> str:
    """"auth" | "race" | "unknown" — см. докстринг модуля. Чистая функция,
    без сети: доказывается мутацией на синтетических строках stderr."""
    text = (stderr or "").lower()
    if any(marker in text for marker in AUTH_FAILURE_MARKERS):
        return "auth"
    if any(marker in text for marker in RACE_MARKERS):
        return "race"
    return "unknown"


# ── Git-транспорт ──────────────────────────────────────────────────────────


def git(*args: str, cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {proc.stderr.strip()[:300]}")
    return proc


def origin_url(repo_env: str = "GITHUB_REPOSITORY") -> str:
    return f"https://github.com/{os.environ[repo_env]}.git"


def branch_exists_on_origin(workdir: str, data_branch: str) -> bool:
    return git("rev-parse", "--verify", "--quiet", f"origin/{data_branch}",
               cwd=workdir, check=False).returncode == 0


def clone_data_branch(origin: str, workdir: str, data_branch: str) -> None:
    """Свежий клон под данные: main + ветка данных, либо просто main, если
    ветки данных на origin ещё нет вовсе (путь первого создания — #882,
    требование 3: ветки может не существовать, этот путь обязан работать).

    `fetch`/`rev-parse` здесь — легитимная РАЗВЕДКА («существует ли ветка»),
    не восстановление после отказа — check=False здесь безопасен: последующий
    `checkout -B` отрабатывает в обеих ветках исхода (создаёт локальную ветку
    от main, если данных ещё нет, либо от уже существующей ветки данных)."""
    git("clone", "--quiet", "--no-tags", origin, workdir, cwd=".")
    git("fetch", "--quiet", "origin", data_branch, cwd=workdir, check=False)
    exists = branch_exists_on_origin(workdir, data_branch)
    base = f"origin/{data_branch}" if exists else "origin/main"
    git("checkout", "--quiet", "-B", data_branch, base, cwd=workdir)


def append_and_push(
    workdir: str,
    data_branch: str,
    rel_path: str,
    commit_identity: tuple[str, ...],
    commit_message: str,
    is_duplicate: Callable[[str], bool],
    render_next: Callable[[str], str],
    retries: int = 5,
) -> bool:
    """Запись файла (через `render_next(текущее_содержимое) -> новое
    содержимое`) + push с ретраем на гонку. Возвращает True, если строка
    записана и запушена; False — `is_duplicate` признал текущее содержимое
    (уже несущее нужную запись) дублем, писать не нужно.

    `is_duplicate`/`render_next` вызываются на содержимом рабочего дерева
    ПОСЛЕ громкого (`check=True`) `fetch`+`checkout` при повторе — то есть на
    ФАКТЕ, только что подтверждённом с сервера, не на устаревшей локальной
    копии (#882, требование 1: «чужая запись существует» обязана
    подтверждаться фактом с сервера).

    Отказ push классифицируется `classify_push_failure`:
      - "auth"/"unknown" — громкий немедленный `RuntimeError`, повтора нет
        (тот же токен даст тот же отказ снова — это красный шаг, не гонка);
      - "race" — `fetch`+`checkout` ГРОМКИЕ (никакого `check=False`): если
        ветка на origin всё ещё не существует, конкурента, выигравшего
        гонку, физически нет — тоже громкий `RuntimeError`, не тихий цикл."""
    path = Path(workdir) / rel_path
    for _ in range(retries):
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if is_duplicate(current):
            return False
        new_text = render_next(current)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as file:
            file.write(new_text)
        git(*commit_identity, "add", rel_path, cwd=workdir)
        git(*commit_identity, "commit", "--quiet", "-m", commit_message, cwd=workdir)
        push = git("push", "--quiet", "origin", f"HEAD:{data_branch}", cwd=workdir,
                   check=False)
        if push.returncode == 0:
            return True

        kind = classify_push_failure(push.stderr)
        if kind != "race":
            raise RuntimeError(
                f"push на {data_branch} отклонён ({kind}, не гонка параллельного "
                f"писателя) — красный шаг, не тихий повтор: {push.stderr.strip()[:300]}"
            )

        # Гонка — перечитываем ФАКТ С СЕРВЕРА: fetch/checkout здесь ГРОМКИЕ,
        # тихий откат этого шага (check=False) и был причиной инцидента #882.
        git("fetch", "--quiet", "origin", data_branch, cwd=workdir)
        if not branch_exists_on_origin(workdir, data_branch):
            raise RuntimeError(
                f"push на {data_branch} отклонён как гонка, но ветка {data_branch} "
                "всё ещё не существует на origin — конкурента, способного выиграть "
                "гонку, нет; это отказ авторизации/прав, а не гонка"
            )
        git("checkout", "--quiet", "-B", data_branch, f"origin/{data_branch}", cwd=workdir)
    raise RuntimeError(f"не удалось записать на {data_branch} за {retries} попыток")
