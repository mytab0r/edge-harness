#!/usr/bin/env python3
"""Завести задачи в беклог из AI-ревью — одной командой (#18).

    python scripts/review/file_tasks.py --pr 140 [--dry-run]

Читает ПОСЛЕДНЕЕ ревью-комментарий в PR (шапка-факты pr/head/reviewer до
первого пустой строки), достаёт канонические блоки ````задача и создаёт issue
с меткой task на каждый. Идемпотентен по заголовку: задача с таким же
заголовком уже открыта — пропускается (повтор команды не дублирует пул).

Тело задачи — как требует шаблон пула: цель + критерий готовности; ревьюер
обязан дать их в блоке (контракт ai_prompt.md).

Среда: gh с правами issues:write (GH_TOKEN или gh auth login).
"""

import argparse
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Разбор комментария/ответа — те же функции, что строит транспорт: одно
# место правды на формат (ai_review.py).
_spec = importlib.util.spec_from_file_location("ai_review", SCRIPT_DIR / "ai_review.py")
ai_review = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ai_review)


def gh(*args: str):
    return ai_review.gh(*args)


def _pages(url_head: str):
    """Листает список GitHub API до короткой страницы. per_page=100 без
    листания молча терял хвосты (>100 комментариев — «последний» ревью
    не находился; >100 задач — рушилась идемпотентность)."""
    page = 1
    while True:
        # separator зависит от того, есть ли уже запрос в URL (замер:
        # голый путь с «&» даёт тихий 404)
        sep = "&" if "?" in url_head else "?"
        chunk = gh(f"{url_head}{sep}per_page=100&page={page}")
        if not isinstance(chunk, list) or not chunk:
            return
        yield from chunk
        if len(chunk) < 100:
            return
        page += 1


def latest_review_comment(repo: str, pr: int) -> dict | None:
    """Последний комментарий AI-ревью: шапка-факты с решающим reviewer:,
    опубликованный доверенной учёткой (review_labels._is_trusted_verdict_author
    — то же место правды, что читает review_labels.latest_ai_comment).
    Репозиторий публичный: посторонний участник может опубликовать
    комментарий с валидной шапкой reviewer: (находка дыры безопасности,
    вердикт ai-review PR #294) — без проверки автора file_tasks завёл бы
    задачи из чужого, не реального ревью. Endpoint комментариев НЕ
    поддерживает sort/direction (всегда по созданию, asc — замер на PR #138:
    desc-параметры молча игнорируются) — листаем все страницы и берём
    ПОСЛЕДНЕЕ совпадение среди доверенных."""
    latest = None
    for comment in _pages(f"repos/{repo}/issues/{pr}/comments"):
        if not ai_review.review_labels._is_trusted_verdict_author(comment):
            continue
        facts = ai_review.header_facts(comment.get("body") or "")
        if facts.get("reviewer") in ("approve", "rework", "error"):
            latest = comment
    return latest


def open_task_titles(repo: str) -> set[str]:
    return {
        issue["title"] for issue in _pages(f"repos/{repo}/issues?state=open&labels=task")
        if "pull_request" not in issue
    }


def file_task(repo: str, task: dict) -> int:
    """Создать issue с меткой task; ответ API — созданный issue (номер из него,
    а не из догадок по спискам — гонка невозможна по построению)."""
    body = task["body"] or "(тело не предложено ревью — уточни цель и критерий готовности)"
    created = gh("-X", "POST", f"repos/{repo}/issues",
                 "-f", f"title={task['title']}",
                 "-f", "body=" + body,
                 "-f", "labels[]=task")
    return int(created["number"])


def current_repo() -> str:
    """Репозиторий: в CI даёт GITHUB_REPOSITORY, локально — gh repo view.
    Отказ громкий: «не понял, куда заводить» лучше неугаданного репо."""
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh repo view: {result.stderr.strip()}")
    return result.stdout.strip()


def filed_marker(body: str) -> list[int]:
    """Маркер заведённого в последней строке комментария: «filed: #139 #140».
    Состояние живёт в самом ревью-комментарии (паттерн Harness pr_loop):
    список issues имеет eventual-consistency окно в секунды — свежесозданные
    задачи не видны повторному запуску, и матч по заголовку плодил дубли
    (поймано живым прогоном #18: #141/#142 дублями #139/#140)."""
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return []
    # ТОЛЬКО последняя непустая строка: строка «filed: #N» внутри фенса задачи
    # (контент модели) маркером не является — проверено ревью PR #138.
    match = re.match(r"^filed:\s*((?:#\d+\s*)+)$", lines[-1])
    if not match:
        return []
    return [int(n) for n in re.findall(r"#(\d+)", match.group(1))]


def mark_filed(repo: str, comment: dict, numbers: list[int]) -> None:
    body = (comment.get("body") or "").rstrip("\n")
    gh("-X", "PATCH", f"repos/{repo}/issues/comments/{comment['id']}",
       "-f", "body=" + body + "\n\nfiled: " + " ".join(f"#{n}" for n in numbers))


def main() -> int:
    parser = argparse.ArgumentParser(description="Завести задачи из AI-ревью PR в пул")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true", help="показать, что будет заведено, не создавая")
    args = parser.parse_args()
    repo = current_repo()

    comment = latest_review_comment(repo, args.pr)
    if comment is None:
        print(f"::error::в PR #{args.pr} нет комментария AI-ревью — заводить не из чего")
        return 1
    facts = ai_review.header_facts(comment.get("body") or "")
    tasks = ai_review.tasks_from_comment(comment.get("body") or "")
    if not tasks:
        print(f"Ревью {facts.get('reviewer')} при head {facts.get('head', '?')[:12]} "
              f"не предложило задач — пул не тронут")
        return 0

    already = filed_marker(comment.get("body") or "")
    if already:
        print(f"Ревью {facts.get('reviewer')} при head {facts.get('head', '?')[:12]}: "
              f"задачи уже заведены ({' '.join(f'#{n}' for n in already)}) — идемпотентность по маркеру")
        return 0

    existing = open_task_titles(repo)
    print(f"Ревью {facts.get('reviewer')} при head {facts.get('head', '?')[:12]}: "
          f"{len(tasks)} предложенных задач, открытых с меткой task: {len(existing)}")
    filed: list[int] = []
    for task in tasks:
        if task["title"] in existing:
            print(f"  = «{task['title']}» — уже открыта, пропущено (идемпотентность)")
            continue
        if args.dry_run:
            print(f"  [dry-run] завёл бы: «{task['title']}»")
            continue
        number = file_task(repo, task)
        filed.append(number)
        print(f"  + #{number} «{task['title']}»")
    if args.dry_run:
        return 0
    if filed:
        # Маркер — ПОСЛЕ создания (атомарности нет, но окно дублей сужено до
        # параллельных запусков команды, чего человек не делает).
        mark_filed(repo, comment, filed)
        print(f"filed: {' '.join(f'#{n}' for n in filed)} — маркер в ревью-комментарии")
    print(f"Готово: заведено {len(filed)}, пропущено {len(tasks) - len(filed)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::file_tasks: {error}")
        sys.exit(1)
