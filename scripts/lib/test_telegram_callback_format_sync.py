#!/usr/bin/env python3
"""Гвардия синхронности формата кнопки решения владельца между Python и TS
(находка ревью PR #486, второй заход).

Класс проблемы: `cf-worker/src/config.ts::TELEGRAM.decisionCommentPrefix` и
`scripts/orchestra/pulse_guard.py::DECISION_COMMENT_PREFIX` (аналогично —
`callbackPrefix`/`OWNER_DECISION_CALLBACK_PREFIX`) обязаны нести ОДНО и то же
значение — TS разбирает callback_data (`parseOwnerDecisionCallback`), Python
его строит (`build_decision_keyboard`) и пишет комментарий-решение
(`apply_owner_decision.py`). Комментарии в обоих файлах утверждали
«расхождение ловится тестом обеих сторон по одному и тому же примеру» — это
было НЕПРАВДОЙ: юнит-тесты каждой стороны прибиты к своим же литералам
(`cf-worker/test/harness.spec.ts` проверяет `"wo:471:1"` как строку, ожидая
её от `pulse_guard`; `test_pulse_guard.py` проверяет тот же литерал со своей
стороны) — ни один тест не читал ОБА исходника одновременно, поэтому
рассинхрон прошёл бы CI зелёным и вскрылся бы в проде кнопкой «Не понял
формат ответа» (парсер cf-worker отверг бы чужой префикс). Этот файл читает
оба исходника как текст (импорт `.ts` в Python невозможен) и сравнивает
значения — обещание из комментариев теперь исполнено, а не декларативно.

Честный потолок: сверяются только СТРОКОВЫЕ константы (префиксы). Лимит 64
байта Bot API — свойство самого Telegram, а не выбор этого репозитория:
Python использует его как assert в `build_decision_keyboard`, TS его нигде
не проверяет числом (парсинг входящего callback_data не обязан знать лимит
исходящего) — сверять здесь нечего, только упоминание в прозе.

Запуск: python -m pytest scripts/lib/test_telegram_callback_format_sync.py -q
"""

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_TS = REPO_ROOT / "cf-worker" / "src" / "config.ts"
PULSE_GUARD_PY = REPO_ROOT / "scripts" / "orchestra" / "pulse_guard.py"

_spec = importlib.util.spec_from_file_location("pulse_guard", PULSE_GUARD_PY)
pulse_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pulse_guard)  # type: ignore[union-attr]


def _ts_string_const(name: str) -> str:
    """Значение `<name>: "..."` внутри `export const TELEGRAM = {...}` в
    config.ts — узкий регэксп по одному конкретному объекту, не разбор TS."""
    text = CONFIG_TS.read_text(encoding="utf-8")
    match = re.search(rf'\b{re.escape(name)}:\s*"([^"]+)"', text)
    assert match, f"config.ts: не нашёл `{name}: \"...\"` — формат объявления изменился?"
    return match.group(1)


def test_callback_prefix_matches_ts_config():
    assert pulse_guard.OWNER_DECISION_CALLBACK_PREFIX == _ts_string_const("callbackPrefix")


def test_decision_comment_prefix_matches_ts_config():
    assert pulse_guard.DECISION_COMMENT_PREFIX == _ts_string_const("decisionCommentPrefix")
