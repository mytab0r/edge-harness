"""Живой `gh` из теста невозможен — fail loud вместо тихого выхода в сеть (#1438).

Чем оплачено. Замер 2026-09-22 на полном прогоне `scripts/orchestra`
(подмена `subprocess.run`, счёт вызовов `gh`): **19 тестов** реально ходили
в GitHub. Из них:

* `test_main_skips_generic_worker_dispatch_when_conflict_rework_already_dispatched`
  — 52 запроса к НАСТОЯЩЕМУ `repos/mytab0r/edge-harness/...`, из того же
  бюджета установки, чьё исчерпание красит живые PR чужой причиной (#1437);
* `test_accept_merged_tasks_ok_close_appends_session_note` — изменяющий
  вызов `gh api -X DELETE repos/o/r/git/refs/locks/task-320`. Спасает
  только то, что репозиторий выдуман. Имя репозитория — совпадение, а не
  предохранитель;
* `test_main_makes_zero_mutating_calls_on_fully_empty_queue` — 21 живой
  вызов у теста, который сторожит ИМЕННО отсутствие изменяющих вызовов.
  Гвардия утекала мимо механизма, который охраняет.

Механизм утечки — не забытый мок, а РАЗНЫЕ ЭКЗЕМПЛЯРЫ модуля в одном
прогоне: `test_scheduler.py` грузит `scheduler` через
`spec_from_file_location`, а `upstream_drift` внутри себя делает
`import scheduler` и получает другой объект. Тест патчит `gh` у своего,
прод-код зовёт `gh` у чужого. Замер:

    sys.modules['scheduler'] id= 140137492084464
    scheduler, который видит upstream_drift, id= 9604480
    тот же gh?  False

Класс уже был описан в шапке `test_mechanical_rebase.py` и решён там для
одного файла; здесь он закрыт для всех.

## Почему гвардия, а не «починим тесты»

Починка девятнадцати утечек — работа на несколько заходов, а класс надо
закрыть сегодня: НОВЫЙ тест не должен иметь возможности уйти в сеть молча.
Поэтому список `ALLOWED_LIVE_GH` фиксирует ровно нынешний долг, с причиной
на запись, и может только СОКРАЩАТЬСЯ — тест, которого в нём нет, падает
громко.

## Газ

Запись снимается из списка вместе с починкой теста. Обратное добавление —
осознанное решение с причиной в этом же файле, а не умолчание.

## Чего этот файл НЕ делает, и почему — по замеру, а не по осторожности

Напрашивающаяся «общая» починка — патчить `gh` не по рукописному списку
модулей (`patch_gh` в test_scheduler.py знает три: scheduler, pulse_guard,
stall_detector), а обнаружением: у КАЖДОГО загруженного модуля репозитория,
у которого есть свой `gh`. Я это реализовал и прогнал: **277 failed,
153 passed** против 430 passed на базе.

То есть рукописный список здесь НЕСУЩИЙ, а не забытый: у остальных модулей
`gh` участвует в сценариях, которые стенд `FakeGh` не покрывает, и подмена
меняет их поведение. Общая замена — не «доведение до конца», а поломка.

Вывод, за который заплачено прогоном: девятнадцать утечек чинятся ПОШТУЧНО,
с разбором того, какой именно объект зовёт прод-код в каждом случае. Этот
файл делает ровно то, что можно сделать надёжно сегодня: закрывает класс
для НОВЫХ тестов и превращает нынешний долг из невидимого в перечисленный.
"""

from __future__ import annotations

import subprocess

import pytest

# Тесты, которые СЕГОДНЯ уходят в живую сеть (замер 2026-09-22, #1438).
# Ключ — nodeid без файла: имя теста. Значение — причина, по которой запись
# ещё здесь. Список только сокращается.
_DEBT_REASON = (
    "утечка патча через второй экземпляр модуля scheduler (#1438) — "
    "чинится приведением тестов к общему sys.modules, запись снимается вместе с починкой"
)
ALLOWED_LIVE_GH: dict[str, str] = {
    name: _DEBT_REASON
    for name in (
        "test_accept_merged_tasks_ok_close_appends_session_note",
        "test_accept_merged_tasks_fail_appends_session_note",
        "test_unhealthy_pulls_appends_session_note",
        "test_main_exits_nonzero_and_escalates_on_archive_hard_failure",
        "test_merge_tail_failure_does_not_speak_about_the_archive_queue",
        "test_main_exits_nonzero_and_escalates_on_stall_hard_failure",
        "test_main_stays_green_when_archive_ok",
        "test_main_exits_nonzero_when_acceptance_hard_failure",
        "test_main_skips_generic_worker_dispatch_when_conflict_rework_already_dispatched",
        "test_after_merge_announces_only_own_branch_task_not_prose_mentions",
        "test_after_merge_telegram_miss_is_loud_but_not_fatal",
        "test_main_still_dispatches_worker_for_rework_when_wip_gate_closed",
        "test_main_skips_worker_dispatch_while_fuse_paused",
        "test_main_makes_zero_mutating_calls_on_fully_empty_queue",
        "test_main_labels_old_unclaimed_task_end_to_end",
        "test_new_stale_episode_alerts_and_carries_the_marker",
        "test_continuing_stale_episode_neither_alerts_nor_reddens",
        "test_main_runs_the_catch_up_pass_every_pulse",
        "test_main_reddens_when_the_catch_up_pass_itself_is_broken",
    )
}

_REAL_RUN = subprocess.run


def _command_is_gh(cmd) -> bool:
    """Первый элемент команды — `gh`. Форма списка и форма строки обе
    встречаются в этом репозитории, поэтому разбираются обе; незнакомая
    форма считается НЕ gh (гвардия не должна падать на чужом вызове —
    её мишень узкая)."""
    try:
        if isinstance(cmd, (list, tuple)):
            first = str(cmd[0]) if cmd else ""
        elif isinstance(cmd, str):
            first = cmd.split()[0] if cmd.split() else ""
        else:
            return False
    except Exception:
        return False
    return first == "gh" or first.endswith("/gh")


@pytest.fixture(autouse=True)
def forbid_live_gh(request, monkeypatch):
    """Любой `subprocess.run(["gh", …])` из теста — громкая ошибка.

    Не «замедляет», не «предупреждает»: выход в сеть из теста делает
    результат зависящим от чужого состояния, квоты и прав, то есть
    перестаёт быть тестом. Разрешённым остаётся только то, что перечислено
    в ALLOWED_LIVE_GH с причиной."""
    if request.node.name.split("[")[0] in ALLOWED_LIVE_GH:
        return

    def guarded(cmd, *args, **kwargs):
        if _command_is_gh(cmd):
            shown = " ".join(map(str, cmd)) if isinstance(cmd, (list, tuple)) else str(cmd)
            raise AssertionError(
                "тест ушёл в ЖИВОЙ GitHub: " + shown[:200] + "\n"
                "Это не замедление, а потеря свойства теста: результат начинает "
                "зависеть от сети, прав и квоты установки (#1437).\n"
                "Обычная причина — патч утёк мимо прод-кода: модуль под тестом "
                "держит СВОЙ экземпляр scheduler (#1438, см. докстринг этого файла "
                "и шапку test_mechanical_rebase.py). Патчить надо тот объект, "
                "который реально зовёт прод-код.\n"
                "Осознанное исключение — запись в ALLOWED_LIVE_GH с причиной."
            )
        return _REAL_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)
