#!/usr/bin/env python3
"""Гвардия по исходнику (замечание второго гейта, PR #656, критерий готовности
№3 задачи #637): «Починил случай — закрой класс».

Класс фраз «нужен человек»/«требуется решение»/«конвейер стоит» — сигнал
конвейеру: дальше без решения владельца нельзя. До #637 такая фраза попадала
ТОЛЬКО в `GITHUB_STEP_SUMMARY` (у него нет читателя, кроме того, кто сам
открыл лог прогона, см. docstring stall_detector.py) — сама задача чинила
ОДИН экземпляр (потолок автозаведения), но статического разбора исходников
на весь класс не существовало (ревью PR #656, раунд 2). Проверено: живых
сайтов этого класса на момент ревью — ровно три (`grep -rn` по
`scripts/`): `stall_detector.py::detect_and_act`,
`scheduler.py::dispatch_conflict_rework`, `scheduler.py::trigger_ai_review`.

Эта гвардия делает четвёртый такой сайт статически невозможным без
осознанного решения: для КАЖДОЙ функции верхнего уровня `stall_detector.py`/
`scheduler.py`, чей исходник содержит фразу класса, требуется либо вызов
`escalate(...)`/`send_telegram(...)`/любого локального помощника с «escalat»
в имени (`_escalate_cap_exhaustion`, `escalate_stale_auto_tasks`, …ровно тот
нейминг, которого этот репозиторий уже держится) — В ТОЙ ЖЕ ФУНКЦИИ, либо
запись в WHITELIST ниже с непустым обоснованием, почему доставка сознательно
не нужна именно в этой функции. Приём — по образцу
test_pulse_guard.py::test_last_failure_error_shares_failing_jobs_not_second_copy
(inspect.getsource + assert по исходнику, не пересказ поведения).

Честная граница: проверка эвристическая (по имени вызываемой функции в
исходном тексте, не по реальному графу вызовов через AST/типы) — подделать
её невозможным вызовом-пустышкой `escalate_looking_but_noop()` в этом
маленьком, дисциплинированном по нейммингу модуле труда не стоит; цель —
не дать МОЛЧА повторить класс #637, а не доказать теорему.

Запуск: python -m pytest scripts/orchestra/test_escalation_class_guard.py -q
"""

import importlib.util
import inspect
import re
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # stall_detector.py/scheduler.py делают `from pulse_guard import …`

SD_SPEC = importlib.util.spec_from_file_location("stall_detector", _DIR / "stall_detector.py")
sd = importlib.util.module_from_spec(SD_SPEC)
SD_SPEC.loader.exec_module(sd)  # type: ignore[union-attr]

SCH_SPEC = importlib.util.spec_from_file_location("scheduler", _DIR / "scheduler.py")
sch = importlib.util.module_from_spec(SCH_SPEC)
SCH_SPEC.loader.exec_module(sch)  # type: ignore[union-attr]

MODULES = (sd, sch)

# Фраза класса — сигнал «конвейер остановился, дальше без человека нельзя»
# (не «просто ждём», не «пропускаю кандидата» — это отдельные, штатные ветки).
_PHRASE_RE = re.compile("нужен человек|требуется решение|конвейер стоит")

# Вызов-доставка: escalate(...)/send_telegram(...) или локальный помощник,
# чьё ИМЯ содержит «escalat» (весь такой нейминг в этих двух модулях СЕГОДНЯ:
# escalate, _escalate_cap_exhaustion, escalate_stale_auto_tasks) — эвристика
# по имени, не по графу вызовов (см. докстринг модуля).
_DELIVERY_RE = re.compile(r"escalat\w*\(|send_telegram\(")

# Явный белый список: функция содержит фразу класса, но саму доставку в ЭТОЙ
# функции заводить не нужно — обоснование обязательно (проверяется ниже как
# непустая строка). Ключ — (имя модуля, имя функции).
WHITELIST: dict[tuple[str, str], str] = {
    ("scheduler", "trigger_ai_review"): (
        "исчерпание бюджета авто-повтора ai-review (#196) — то же состояние, "
        "что независимо ловит repo_invariants.check_stuck_review_gate "
        "(инвариант 3, #472) и уже доставляет владельцу через escalate "
        "(repo_invariants.run_escalations, канал WATCHDOG_ISSUE/#120) — с "
        "БОЛЕЕ полным фактом (возраст текущей эпохи гейта 1, бюджет по "
        "эпохам отдельно от перенесённого из старой, был ли вердикт ai:* "
        "хоть раз за жизнь PR). Второй, беднее контекстом вызов escalate "
        "прямо здесь дал бы дублирующую эскалацию ОДНОГО и того же состояния "
        "двумя независимыми путями, а не закрыл бы пробел — пробела нет."
    ),
}


def _own_functions(module):
    """Функции ВЕРХНЕГО уровня модуля, определённые в НЁМ САМОМ (не
    импортированные из другого модуля — inspect.getmembers видит и их тоже,
    фильтр по __module__ убирает копии вроде escalate/gh/post_issue_comment,
    попавшие в module.__dict__ через `from pulse_guard import ...`)."""
    return {
        name: obj for name, obj in inspect.getmembers(module, inspect.isfunction)
        if obj.__module__ == module.__name__
    }


def test_every_class_phrase_site_has_delivery_or_whitelist_reason():
    checked: list[str] = []
    for module in MODULES:
        for name, func in sorted(_own_functions(module).items()):
            source = inspect.getsource(func)
            if not _PHRASE_RE.search(source):
                continue
            checked.append(f"{module.__name__}.{name}")
            key = (module.__name__, name)
            reason = WHITELIST.get(key)
            has_delivery = bool(_DELIVERY_RE.search(source))
            assert has_delivery or reason, (
                f"{module.__name__}.{name} содержит фразу класса «нужен человек»/"
                "«требуется решение»/«конвейер стоит», но не вызывает escalate/"
                "send_telegram в этой же функции и не занесена в WHITELIST с "
                "обоснованием (см. docstring этого файла) — реши судьбу сайта "
                "(доставить или осознанно исключить), прежде чем мержить"
            )
            if reason is not None:
                assert reason.strip(), f"{key} в WHITELIST без обоснования — пустая строка не считается решением"

    # Пин известного набора: ровно 3 сайта на момент ревью PR #656. Новый
    # сайт (checked стал длиннее) — не молчаливый провал, а явное указание
    # обновить этот тест: сначала реши судьбу нового сайта (delivery в той же
    # функции или WHITELIST с обоснованием), потом подними число.
    assert sorted(checked) == [
        "scheduler.dispatch_conflict_rework",
        "scheduler.trigger_ai_review",
        "stall_detector.detect_and_act",
    ], (
        f"набор функций с фразой класса изменился: {sorted(checked)} — если "
        "это НОВЫЙ сайт, реши его судьбу (доставка в той же функции или "
        "WHITELIST с обоснованием) и обнови список здесь; если сайт пропал "
        "(функция удалена/переписана) — тем более обнови список, не оставляй "
        "тест утверждать неправду"
    )
