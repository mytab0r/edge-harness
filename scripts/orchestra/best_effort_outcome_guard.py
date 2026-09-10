#!/usr/bin/env python3
"""Реальный outcome continue-on-error шагов orchestra.yml, не conclusion (#887).

Класс: `continue-on-error: true` в `.github/workflows/orchestra.yml` нарочно
держит job `orchestra` зелёным при провале best-effort гвардий
(`stale_blocked_guard.py`/`waiting_owner_guard.py`/`health_audit.py`) —
`heartbeat_check` (`pulse_guard.py`, #120) читает conclusion ЗАПУСКА orchestra
по расписанию как признак живого пульса, и красный job здесь ложно сигналил
бы «пульс пропал» из-за находки одной из гвардий, а не реальной остановки.

Но тем же приёмом Jobs API маскирует РЕАЛЬНЫЙ провал ШАГА:
`GET .../actions/jobs/{id}` отдаёт `steps[].conclusion` — значение уже ПОСЛЕ
применения `continue-on-error` (всегда `success`), а не `steps.<id>.outcome`
(реальный результат ДО маскировки) — этот `outcome` виден только контекстом
`steps` ВНУТРИ ещё идущего job, не через REST после завершения. Комментарий у
шага «Гвардия протухшей метки blocked» раньше утверждал «красный шаг виден в
логе (fail loud)» — неверно для машинного читателя: живой факт (2026-09-10,
прогон 34506949025) — Jobs API отдал success, лог нёс
`##[error]Process completed with exit code 1`, и ни один механизм эту
разницу не читал, только человек, вручную открывший лог.

Этот шаг — последний в job `orchestra`, `if: always()`, читает
`${{ toJSON(steps) }}` (снимок ДО завершения job, реальные outcome ещё
доступны) и явно эскалирует (тот же канал #120+Telegram, что и остальные
гвардии — pulse_guard.escalate) любой шаг, чей `outcome == 'failure'`, но
`conclusion` уже замаскирован `continue-on-error`. Не требует ручного
перечисления шагов сверх того, что уже есть в workflow: растёт вместе с
job — новый `continue-on-error` шаг подхватывается сам, ЕСЛИ у него есть
явный `id:` (иначе GitHub не кладёт его в контекст `steps` вовсе) — гвардия
`scripts/lib/test_orchestra_workflow_lint.py` требует `id:` у каждого
`continue-on-error` шага orchestra.yml, чтобы это условие не нарушили молча.

Дедуп — тот же приём, что у DEBT_DIGEST/ESCALATING_INVARIANTS
(`repo_invariants.escalate_if_new`): маркер кодирует ТЕКУЩИЙ набор реально
провалившихся id — тот же набор, тот же прогон подряд — тишина, набор
изменился — новая запись.

Запуск:
  STEPS_JSON='{"stale_blocked": {"outcome": "success", ...}}' \
    GITHUB_REPOSITORY=owner/repo python scripts/orchestra/best_effort_outcome_guard.py
  python -m pytest scripts/orchestra/test_best_effort_outcome_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import sys

_PG_SPEC = importlib.util.spec_from_file_location(
    "pulse_guard", Path(__file__).resolve().parent / "pulse_guard.py")
pulse_guard = importlib.util.module_from_spec(_PG_SPEC)
_PG_SPEC.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

_RI_SPEC = importlib.util.spec_from_file_location(
    "repo_invariants", Path(__file__).resolve().parent / "repo_invariants.py")
repo_invariants = importlib.util.module_from_spec(_RI_SPEC)
_RI_SPEC.loader.exec_module(repo_invariants)  # type: ignore[union-attr]


def find_masked_failures(steps: dict) -> list[str]:
    """Чистая функция: `{id: {"outcome": ..., "conclusion": ...}}` (форма
    контекста `steps` GitHub Actions, снятая `toJSON(steps)`) -> отсортированный
    список id, чей реальный `outcome` — `failure`. Не смотрит на `conclusion`
    вовсе (он и есть та маска, которую эта функция обязана игнорировать) —
    репортим по РЕАЛЬНОМУ результату, даже если он же уронил бы job без
    continue-on-error (лишний, но безвредный факт)."""
    if not isinstance(steps, dict):
        return []
    return sorted(
        step_id for step_id, info in steps.items()
        if isinstance(info, dict) and info.get("outcome") == "failure"
    )


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    raw = os.environ.get("STEPS_JSON", "{}")
    try:
        steps = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"::error::best_effort_outcome_guard: STEPS_JSON не разобран: {error}")
        return 1

    masked = find_masked_failures(steps)
    if not masked:
        print("💚 continue-on-error шаги orchestra: реальных провалов не найдено")
        return 0

    key = ",".join(masked)
    text = (
        "🚨 edge-harness: orchestra.yml — реальный провал continue-on-error "
        f"шага (Jobs API этого не покажет — там success): {key}. Лог этого "
        "прогона orchestra называет причину по каждому id."
    )
    result = repo_invariants.escalate_if_new(repo, "best-effort-outcome", key, text)
    if result:
        print(f"📣 эскалирован реальный провал best-effort шага(ов) {key}: {result}")
    else:
        print(f"🔇 реальный провал best-effort шага(ов) {key} — тот же набор уже эскалирован")
    return 0


if __name__ == "__main__":
    sys.exit(main())
