#!/usr/bin/env python3
"""Тесты file_tasks.py: фильтр по МАСШТАБ (#426) — issue заводится только
для находок, помеченных МАСШТАБ: отдельно. Комментарий, из которого читаем
задачи, строится ai_review.build_comment (та же прод-форма, что публикует
шаг verdict ai-review.yml) — не наш пересказ формата.

Запуск: python -m pytest scripts/review/test_file_tasks.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

_spec = importlib.util.spec_from_file_location("file_tasks", _DIR / "file_tasks.py")
fts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fts)  # type: ignore[union-attr]

ai_review = fts.ai_review
REPO = "mytab0r/edge-harness"


def trusted_comment(comment_id: int, body: str) -> dict:
    """Прод-форма: комментарий от сервисной учётки GITHUB_TOKEN самого workflow
    (review_labels.TRUSTED_VERDICT_LOGIN/_is_trusted_verdict_author)."""
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": ai_review.review_labels.TRUSTED_VERDICT_LOGIN, "type": "Bot"},
    }


class FakeGh:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")

    def mutating_calls(self):
        return [c for c in self.calls if c.startswith(("-X POST", "-X PUT", "-X DELETE", "-X PATCH"))]


def patch_gh(monkeypatch, fake):
    monkeypatch.setattr(ai_review, "gh", fake)


def build_verdict_comment(tasks, *, pr=140, head="abc123", verdict="rework"):
    return ai_review.build_comment(pr, head, verdict, "Находки ревью.", tasks)


def test_main_files_only_separate_scope_skips_tail_and_unscoped(monkeypatch, capsys):
    tasks = [
        {"title": "Заводи меня", "body": "Цель.\nКритерий.", "scope": "отдельно"},
        {"title": "Хвост, не заводи", "body": "Тело хвоста.", "scope": "хвост"},
        {"title": "Без масштаба, не заводи", "body": "Тело без поля.", "scope": None},
    ]
    body = build_verdict_comment(tasks)
    comment = trusted_comment(1, body)
    created = {"number": 900}
    fake = FakeGh({
        "issues/140/comments": [comment],
        "issues?state=open&labels=task": [],
        "-X POST repos/mytab0r/edge-harness/issues -f title=Заводи меня": created,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sys, "argv", ["file_tasks.py", "--pr", "140"])
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    code = fts.main()
    assert code == 0
    posts = [c for c in fake.calls if c.startswith("-X POST") and "/issues -f title=" in c]
    # Единственный POST создания issue — «Заводи меня»: build_comment фенсит
    # ТОЛЬКО МАСШТАБ: отдельно, хвост и без-поля уходят прозой и tasks_from_comment
    # их вообще не видит (не наш пересказ — реальная сборка комментария выше).
    assert len(posts) == 1
    assert "title=Заводи меня" in posts[0]
    out = capsys.readouterr().out
    assert "Хвост, не заводи" not in out
    assert "Без масштаба, не заводи" not in out


def test_main_only_tail_and_unscoped_files_nothing(monkeypatch, capsys):
    tasks = [
        {"title": "Хвост один", "body": "Тело.", "scope": "хвост"},
        {"title": "Без поля один", "body": "Тело.", "scope": None},
    ]
    body = build_verdict_comment(tasks)
    comment = trusted_comment(2, body)
    fake = FakeGh({"issues/141/comments": [comment]})
    patch_gh(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(sys, "argv", ["file_tasks.py", "--pr", "141"])
    code = fts.main()
    assert code == 0
    assert not fake.mutating_calls()  # ни одного issue не заведено и маркер не пишется


def test_main_files_multiple_separate_tasks_ignores_mixed_scope(monkeypatch):
    tasks = [
        {"title": "Отдельно раз", "body": "Тело раз.", "scope": "отдельно"},
        {"title": "Хвост между", "body": "Тело хвоста.", "scope": "хвост"},
        {"title": "Отдельно два", "body": "Тело два.", "scope": "отдельно"},
    ]
    body = build_verdict_comment(tasks)
    comment = trusted_comment(3, body)
    fake = FakeGh({
        "issues/150/comments": [comment],
        "issues?state=open&labels=task": [],
        "-X POST repos/mytab0r/edge-harness/issues -f title=Отдельно раз": {"number": 901},
        "-X POST repos/mytab0r/edge-harness/issues -f title=Отдельно два": {"number": 902},
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(sys, "argv", ["file_tasks.py", "--pr", "150"])
    code = fts.main()
    assert code == 0
    posts = [c for c in fake.calls if c.startswith("-X POST") and "/issues -f title=" in c]
    assert len(posts) == 2
    titles = {c.split("title=")[1].split(" -f body=")[0] for c in posts}
    assert titles == {"Отдельно раз", "Отдельно два"}


def test_main_defensively_skips_fenced_task_with_non_separate_scope(monkeypatch, capsys):
    # Защита в глубину (#426): build_comment никогда не фенсит МАСШТАБ != отдельно,
    # но комментарий может быть отредактирован вручную/искажён — file_tasks.py
    # обязан пропустить такой фенс явно, а не завести issue молча.
    body = (
        "pr: 160\nhead: abc\nreviewer: rework\n\n"
        "Находки.\n\n"
        f"{ai_review.TASK_FENCE}\nЗаводи меня\nМАСШТАБ: отдельно\nЦель.\n"
        f"{'`' * 4}\n\n"
        f"{ai_review.TASK_FENCE}\nНе заводи — искажённый фенс\nМАСШТАБ: хвост\nЦель.\n"
        f"{'`' * 4}\n"
    )
    comment = trusted_comment(4, body)
    fake = FakeGh({
        "issues/160/comments": [comment],
        "issues?state=open&labels=task": [],
        "-X POST repos/mytab0r/edge-harness/issues -f title=Заводи меня": {"number": 903},
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(sys, "argv", ["file_tasks.py", "--pr", "160"])
    code = fts.main()
    assert code == 0
    posts = [c for c in fake.calls if c.startswith("-X POST") and "/issues -f title=" in c]
    assert len(posts) == 1
    assert "title=Заводи меня" in posts[0]
    out = capsys.readouterr().out
    assert any("Не заводи — искажённый фенс" in line and "пропущено" in line for line in out.splitlines())
