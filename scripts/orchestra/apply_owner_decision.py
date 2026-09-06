#!/usr/bin/env python3
"""Применение решения владельца, присланного нажатием инлайн-кнопки в Telegram
(#254). Запускается workflow'ом .github/workflows/owner-decision.yml по
repository_dispatch (event_type owner-decision), который морда шлёт узким
GH_DISPATCH_TOKEN (ADR 0008, Contents+Actions, без Issues) — сам этот job
не получает никакого нового секрета, только issues:write из permissions
workflow'а (github.token).

Пишет РОВНО ТОТ ЖЕ артефакт, что и ручной ответ владельца комментарием
(#470/#471, PROTOCOL.md «Решение владельца как артефакт») — первая строка
«РЕШЕНИЕ: N» в задаче issue_number. Дальше решение снимает waiting:owner уже
существующая (по мержу #471) гвардия scripts/orchestra/waiting_owner_guard.py
на следующем пульсе orchestra — второй путь применения здесь НЕ заводится,
переиспользован post_issue_comment из pulse_guard.py без изменений.

Запуск: python scripts/orchestra/apply_owner_decision.py --repo o/r --issue N --option M
"""

import argparse
import sys

from pulse_guard import DECISION_COMMENT_PREFIX, post_issue_comment


def decision_comment(option: int) -> str:
    return (
        f"{DECISION_COMMENT_PREFIX}: {option}\n\n"
        "Источник: нажатие инлайн-кнопки в Telegram (#254)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--issue", required=True, type=int, help="номер задачи (issue), не PR")
    parser.add_argument("--option", required=True, type=int, help="номер выбранного варианта (с 1)")
    args = parser.parse_args(argv)

    post_issue_comment(args.repo, args.issue, decision_comment(args.option))
    print(f"apply_owner_decision: #{args.issue} — РЕШЕНИЕ: {args.option} записано комментарием")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::apply_owner_decision: {error}", file=sys.stderr)
        sys.exit(1)
