#!/usr/bin/env python3
"""Гвардия синхронности формата кнопки решения владельца между Python и TS,
и внутри самого Python (находка ревью PR #486, второй и третий заход).

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

Второй, чисто Python'овский разрыв (третий заход того же ревью): формат
«РЕШЕНИЕ: N», который ПИШЕТ `apply_owner_decision.py::decision_comment`
(построен вокруг `pulse_guard.DECISION_COMMENT_PREFIX`), и формат, который
ЧИТАЕТ `waiting_owner_guard.py::DECISION_MARKER_RE` — второй объявлен
отдельным литералом `"РЕШЕНИЕ"`, не импортирован из `pulse_guard`.
`test_apply_owner_decision.py` проверял только буквальную строку `"РЕШЕНИЕ: 2"`,
`test_waiting_owner_guard.py` — свой собственный литерал в фикстурах; ни один
не прогонял РЕАЛЬНЫЙ вывод `decision_comment()` через РЕАЛЬНЫЙ
`DECISION_MARKER_RE` — расхождение прошло бы тем же путём, что и TS↔Python.

Честный потолок: сверяются только СТРОКОВЫЕ константы (префиксы). Лимит 64
байта Bot API — свойство самого Telegram, а не выбор этого репозитория:
Python использует его как assert в `build_decision_keyboard`, TS его нигде
не проверяет числом (парсинг входящего callback_data не обязан знать лимит
исходящего) — сверять здесь нечего, только упоминание в прозе.

Запуск: python -m pytest scripts/lib/test_telegram_callback_format_sync.py -q
"""

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_TS = REPO_ROOT / "cf-worker" / "src" / "config.ts"
ORCHESTRA_DIR = REPO_ROOT / "scripts" / "orchestra"
PULSE_GUARD_PY = ORCHESTRA_DIR / "pulse_guard.py"
APPLY_OWNER_DECISION_PY = ORCHESTRA_DIR / "apply_owner_decision.py"
WAITING_OWNER_GUARD_PY = ORCHESTRA_DIR / "waiting_owner_guard.py"

# apply_owner_decision.py делает `from pulse_guard import ...` (плоский
# импорт, не относительный) — разрешается, только если каталог с pulse_guard
# уже на sys.path (тот же приём, что test_waiting_owner_guard.py уже
# применяет для того же каталога).
if str(ORCHESTRA_DIR) not in sys.path:
    sys.path.insert(0, str(ORCHESTRA_DIR))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


pulse_guard = _load("pulse_guard", PULSE_GUARD_PY)
apply_owner_decision = _load("apply_owner_decision", APPLY_OWNER_DECISION_PY)
waiting_owner_guard = _load("waiting_owner_guard", WAITING_OWNER_GUARD_PY)


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


def test_decision_comment_output_matches_decision_marker_regex():
    """РЕАЛЬНЫЙ вывод apply_owner_decision.decision_comment(N) обязан
    матчиться РЕАЛЬНЫМ waiting_owner_guard.DECISION_MARKER_RE — не пересказ
    друг друга двумя отдельными литералами в разных тестовых файлах."""
    text = apply_owner_decision.decision_comment(2)
    match = waiting_owner_guard.DECISION_MARKER_RE.search(text)
    assert match is not None, f"DECISION_MARKER_RE не нашёл маркер в {text!r}"
    assert int(match.group(1)) == 2
