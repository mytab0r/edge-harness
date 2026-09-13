#!/usr/bin/env python3
"""Эскалация автооткота `deploy-worker.yml` при красной канарейке UI на проде
(#614, дефект: откат был, эскалации не было).

`.github/workflows/deploy-worker.yml` деплоит cf-worker (морда/API/журнал) и
после деплоя гоняет канарейку UI (Playwright, `cf-worker/scripts/canary-ui.mjs`).
До этого файла красная канарейка красила прогон и молчала: прод оставался на
сломанной версии до ручного вмешательства, без единого следа в Telegram/#120 —
в отличие от почти всех остальных механизмов репозитория. Автооткат сам
(`wrangler rollback --yes`) и повторная канарейка после него — шаги workflow,
по образцу уже проверенного в проде `deploy-dsh-edge.yml` (issue #549/#562).
Этот модуль — только текст и канал сигнала о том, ЧТО произошло: канал общий
с предохранителем конвейера (`pulse_guard.escalate`, issue `WATCHDOG_ISSUE`
+ Telegram) — второй канал для того же класса «поломка после деплоя» не
заводим (тот же принцип, что уже применяют `branch_protection_watch.py` и
`scripts/measure/quotas.py`).

Три исхода (см. `rollback_alert_text`), от тише к громче:
1. rollback_confirmed=True, post_rollback_ok=True — откат сработал, прод жив
   на предыдущей версии: информационный сигнал, не паника.
2. rollback_confirmed=False — `wrangler rollback` не подтвердил успех (лог не
   содержит «has been deployed to 100% of traffic», тот же критерий, что
   у dsh-edge) — прод, возможно, всё ещё на сломанной версии.
3. rollback_confirmed=True, post_rollback_ok=False — откат прошёл, но
   ПОВТОРНАЯ проверка после него красная: живость откатанной версии не
   подтверждена — худший случай (владелец, задача #614, п.1), обязан быть
   самым громким.

Кроме исхода, текст называет ТРИГГЕР честно (`canary_ran`, находка
четвёртого гейта PR #617, правило «Алерт не гадает»): шаги отката/эскалации
срабатывают при ЛЮБОМ провале после деплоя, включая «Секреты воркера», после
которого канарейка не выполнялась вовсе, — данные шага различают «покраснел
шаг канарейки» и «канарейка не добежала», поэтому текст обязан различать их
сам, а не утверждать «канарейка красная» безусловно.

Вызывается ТОЛЬКО когда деплой прошёл и что-то после него упало (тот же
guard `if: failure() && steps.deploy.outcome == 'success'`, что у самого
шага отката) — сценарий «деплой не удался вовсе» этим модулем не описывается
(откатывать нечего, см. workflow)."""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---


import os
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from pulse_guard import WATCHDOG_ISSUE, escalate

MARKER = "[статус: автооткат cf-worker]"


def parse_bool_env(value: str | None) -> bool:
    """Форма вывода GitHub Actions (`steps.<id>.outputs.x`/сравнение `==`) —
    строка `"true"`/`"false"` (регистронезависимо); отсутствие/пусто — False,
    не гадаем в пользу тревоги или тишины произвольно, false — нейтральный
    дефолт, безопасный в обеих ветках вызова (см. докстринг модуля)."""
    return (value or "").strip().lower() == "true"


def trigger_clause(canary_ran: bool) -> str:
    """Честное название триггера («Алерт не гадает», AGENTS.md): факт
    формулируется на той гранулярности, которую дают данные шага —
    «шаг канарейки покраснел», а не «сценарий канарейки красный» (внутри
    шага может упасть и восстановление окружения)."""
    if canary_ran:
        return "триггер: покраснел ШАГ «Канарейка UI на проде»"
    return (
        "триггер: упали шаги ДО канарейки UI (например, «Секреты воркера») — "
        "канарейка не выполнялась"
    )


def rollback_alert_text(
    repo: str,
    run_id: str,
    rollback_confirmed: bool,
    post_rollback_ok: bool,
    canary_ran: bool,
    server_url: str = "https://github.com",
) -> str:
    run_url = f"{server_url}/{repo}/actions/runs/{run_id}" if run_id else "без ссылки"
    clause = trigger_clause(canary_ran)
    if not rollback_confirmed:
        return (
            f"🚨 edge-harness: {MARKER}\n"
            "Деплой cf-worker прошёл, но job после него покраснел, АВТООТКАТ "
            "НЕ ПОДТВЕРЖДЁН (`wrangler rollback` не отчитался успехом в логе шага) "
            "— прод, возможно, остался на сломанной версии. Нужно РУЧНОЕ "
            "ВМЕШАТЕЛЬСТВО НЕМЕДЛЕННО: см. лог job'а, шаг «Автооткат прода при "
            "красной канарейке».\n"
            f"{clause}\n"
            f"Прогон: {run_url}"
        )
    if not post_rollback_ok:
        return (
            f"🚨 edge-harness: {MARKER}\n"
            "Автооткат cf-worker выполнен (`wrangler rollback` подтвердил 100% "
            "трафика), но ПОВТОРНАЯ проверка ПОСЛЕ отката покраснела — живость "
            "откатанной версии НЕ подтверждена. Худший случай: нужно РУЧНОЕ "
            "ВМЕШАТЕЛЬСТВО НЕМЕДЛЕННО.\n"
            f"{clause}\n"
            f"Прогон: {run_url}"
        )
    return (
        f"🔙 edge-harness: {MARKER}\n"
        "Job деплоя cf-worker покраснел после деплоя — прод автоматически "
        "откачен на предыдущую 100%-версию, повторная канарейка после отката "
        "прошла.\n"
        f"{clause}\n"
        f"Прогон: {run_url}"
    )


def escalate_rollback(
    repo: str,
    run_id: str,
    rollback_confirmed: bool,
    post_rollback_ok: bool,
    canary_ran: bool,
    server_url: str = "https://github.com",
) -> str:
    """Канал — тот же, что предохранитель конвейера (#120 + Telegram,
    `pulse_guard.escalate`), см. докстринг модуля."""
    text = rollback_alert_text(
        repo, run_id, rollback_confirmed, post_rollback_ok, canary_ran, server_url
    )
    return escalate(repo, WATCHDOG_ISSUE, text)


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    rollback_confirmed = parse_bool_env(os.environ.get("ROLLBACK_CONFIRMED"))
    post_rollback_ok = parse_bool_env(os.environ.get("POST_ROLLBACK_OK"))
    canary_ran = parse_bool_env(os.environ.get("CANARY_RAN"))
    result = escalate_rollback(
        repo, run_id, rollback_confirmed, post_rollback_ok, canary_ran, server_url
    )
    print(f"Эскалация автооткота deploy-worker: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
