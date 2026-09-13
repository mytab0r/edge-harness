#!/usr/bin/env python3
"""Применение решения владельца, присланного нажатием инлайн-кнопки в Telegram
(#254). Запускается workflow'ом .github/workflows/owner-decision.yml по
repository_dispatch (event_type owner-decision), который морда шлёт узким
GH_DISPATCH_TOKEN (ADR 0008, Contents+Actions, без Issues) — сам этот job
не получает никакого нового секрета, только issues:write из permissions
workflow'а (github.token).

Пишет РОВНО ТОТ ЖЕ артефакт, что и ручной ответ владельца комментарием
(#470/#471, PROTOCOL.md «Решение владельца как артефакт») — первая строка
«РЕШЕНИЕ: N» в задаче issue_number. Дальше решение снимет waiting:owner
гвардия scripts/orchestra/waiting_owner_guard.py (#470/#471, слит) на
следующем пульсе orchestra. Второй путь применения здесь НЕ заводится,
переиспользован post_issue_comment из pulse_guard.py без изменений.

Проверка «задача всё ещё ждёт» (находка ревью PR #486, четвёртый заход):
клавиатуры прошлых эскалаций ничем не удаляются (кнопки снимает только
`editMessageText` по нажатию), а гвардия шлёт НОВОЕ кнопочное сообщение
каждые `WAITING_OWNER_REESCALATE_HOURS` часов — после ответа комментарием
в чате несколько наборов живых кнопок остаются нажимаемыми. Случайное
нажатие протухшей кнопки без этой проверки записало бы «РЕШЕНИЕ: N» в
задачу, уже взятую воркером, закрытую, или переоткрытую под новый раунд с
другой нумерацией вариантов — молча неверный артефакт, выглядящий
авторитетным решением владельца. Морда к этому моменту уже ответила
владельцу «Принято» (answerCallbackQuery по 204 от dispatch) — красный job
здесь единственный видимый сигнал расхождения, поэтому отказ громкий
(RuntimeError → `::error::` → exit 1), не тихий no-op.

Запуск: python scripts/orchestra/apply_owner_decision.py --repo o/r --issue N --option M
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import argparse
import sys

from pulse_guard import DECISION_COMMENT_PREFIX, gh, post_issue_comment
from waiting_owner_guard import WAITING_OWNER_LABEL


def decision_comment(option: int) -> str:
    return (
        f"{DECISION_COMMENT_PREFIX}: {option}\n\n"
        "Источник: нажатие инлайн-кнопки в Telegram (#254)."
    )


def issue_still_waiting(repo: str, issue_number: int) -> bool:
    """Задача открыта И всё ещё несёт waiting:owner — решение по нажатой
    кнопке применимо. False — задача закрыта, или метку уже сняли/сменили
    раунд (см. докстринг модуля)."""
    issue = gh(f"repos/{repo}/issues/{issue_number}")
    if not issue or issue.get("state") != "open":
        return False
    labels = {label["name"] for label in (issue.get("labels") or [])}
    return WAITING_OWNER_LABEL in labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--issue", required=True, type=int, help="номер задачи (issue), не PR")
    parser.add_argument("--option", required=True, type=int, help="номер выбранного варианта (с 1)")
    args = parser.parse_args(argv)

    if not issue_still_waiting(args.repo, args.issue):
        raise RuntimeError(
            f"#{args.issue}: задача не в состоянии waiting:owner (закрыта либо метка "
            "снята/сменился раунд) — решение НЕ записано, нажата протухшая кнопка "
            "прошлой эскалации"
        )

    post_issue_comment(args.repo, args.issue, decision_comment(args.option))
    print(f"apply_owner_decision: #{args.issue} — РЕШЕНИЕ: {args.option} записано комментарием")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::apply_owner_decision: {error}", file=sys.stderr)
        sys.exit(1)
