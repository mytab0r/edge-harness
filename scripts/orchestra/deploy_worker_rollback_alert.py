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
   ПОВТОРНАЯ канарейка после него тоже красная: откатанная версия не
   отвечает — худший случай (владелец, задача #614, п.1), обязан быть
   самым громким.

Вызывается ТОЛЬКО когда деплой прошёл и что-то после него упало (тот же
guard `if: failure() && steps.deploy.outcome == 'success'`, что у самого
шага отката) — сценарий «деплой не удался вовсе» этим модулем не описывается
(откатывать нечего, см. workflow)."""

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


def rollback_alert_text(
    repo: str,
    run_id: str,
    rollback_confirmed: bool,
    post_rollback_ok: bool,
    server_url: str = "https://github.com",
) -> str:
    run_url = f"{server_url}/{repo}/actions/runs/{run_id}" if run_id else "без ссылки"
    if not rollback_confirmed:
        return (
            f"🚨 edge-harness: {MARKER}\n"
            "Канарейка UI cf-worker на проде красная, деплой прошёл, но АВТООТКАТ "
            "НЕ ПОДТВЕРЖДЁН (`wrangler rollback` не отчитался успехом в логе шага) "
            "— прод, возможно, остался на сломанной версии. Нужно РУЧНОЕ "
            "ВМЕШАТЕЛЬСТВО НЕМЕДЛЕННО: см. лог job'а, шаг «Автооткат прода при "
            "красной канарейке».\n"
            f"Прогон: {run_url}"
        )
    if not post_rollback_ok:
        return (
            f"🚨 edge-harness: {MARKER}\n"
            "Канарейка UI cf-worker на проде была красной, автооткат выполнен "
            "(`wrangler rollback` подтвердил 100% трафика), но ПОВТОРНАЯ канарейка "
            "ПОСЛЕ отката ТОЖЕ красная — откатанная версия не отвечает. Худший "
            "случай: нужно РУЧНОЕ ВМЕШАТЕЛЬСТВО НЕМЕДЛЕННО.\n"
            f"Прогон: {run_url}"
        )
    return (
        f"🔙 edge-harness: {MARKER}\n"
        "Канарейка UI cf-worker на проде была красной — прод автоматически "
        "откачен на предыдущую 100%-версию, повторная канарейка после отката "
        "прошла.\n"
        f"Прогон: {run_url}"
    )


def escalate_rollback(
    repo: str,
    run_id: str,
    rollback_confirmed: bool,
    post_rollback_ok: bool,
    server_url: str = "https://github.com",
) -> str:
    """Канал — тот же, что предохранитель конвейера (#120 + Telegram,
    `pulse_guard.escalate`), см. докстринг модуля."""
    text = rollback_alert_text(repo, run_id, rollback_confirmed, post_rollback_ok, server_url)
    return escalate(repo, WATCHDOG_ISSUE, text)


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    rollback_confirmed = parse_bool_env(os.environ.get("ROLLBACK_CONFIRMED"))
    post_rollback_ok = parse_bool_env(os.environ.get("POST_ROLLBACK_OK"))
    result = escalate_rollback(repo, run_id, rollback_confirmed, post_rollback_ok, server_url)
    print(f"Эскалация автооткота deploy-worker: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
