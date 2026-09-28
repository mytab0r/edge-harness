"""Живой `gh` из теста невозможен — fail loud вместо тихого выхода в сеть (#1438).

Чем оплачено. Замер 2026-09-22 на полном прогоне `scripts/orchestra`
(подмена `subprocess.Popen`, счёт вызовов `gh`): **17 тестов** реально ходят
в GitHub. Первый замер делался подменой `subprocess.run` и имел то же слепое
пятно, что и первая редакция гвардии (находка ai-review PR #1447), — список
пересобран. Из тогдашних девятнадцати две записи сняты не белым списком, а
починкой (см. «Что уже починено» ниже). Из оставшихся:

* `test_main_skips_generic_worker_dispatch_when_conflict_rework_already_dispatched`
  — 52 запроса к НАСТОЯЩЕМУ `repos/mytab0r/edge-harness/...`, из того же
  бюджета установки, чьё исчерпание красит живые PR чужой причиной (#1437);
* `test_accept_merged_tasks_ok_close_appends_session_note` — изменяющий
  вызов `gh api -X DELETE repos/o/r/git/refs/locks/task-320`. Спасает
  только то, что репозиторий выдуман. Имя репозитория — совпадение, а не
  предохранитель;
## Что уже починено, а не внесено в список

Два теста-сторожа — `test_main_makes_zero_mutating_calls_on_fully_empty_queue`
и `test_main_labels_old_unclaimed_task_end_to_end` — утекали в сеть и из-за
этого падали на полном прогоне каталога. Первый сторожит ИМЕННО отсутствие
изменяющих вызовов, то есть гвардия утекала мимо механизма, который
охраняет; держать её в белом списке значило бы выключить её насовсем.

Починены точечно: `patch_gh` патчит ещё и тот экземпляр `pulse_guard`,
который держит `upstream_drift`. После этого полный прогон каталога —
**1634 passed** (замер финального дерева), и обоих имён в замере больше нет.

Механизм утечки — не забытый мок, а РАЗНЫЕ ЭКЗЕМПЛЯРЫ модуля в одном
прогоне. Держатель — `pulse_guard` (замер на полном прогоне каталога):

    sys.modules['pulse_guard']            id= 139625455672416
    pulse_guard у upstream_drift          id= 139625481296192
    тот же объект?  False

`upstream_drift_lines` зовёт `pulse_guard.gh(...)`, тест патчит
`sys.modules["pulse_guard"].gh` — и это разные объекты, поэтому вызов уходил
в живую сеть (`repos/pawaca/dsh-edge/tags`).

**Поправка к первой редакции этого файла.** Сначала здесь стояло «второй
экземпляр `scheduler`, который видит `upstream_drift`, id= 9604480». Это
было НЕВЕРНО: `upstream_drift` атрибута `sch` не имеет вовсе, а 9604480 —
`id(None)`, проверено (`python3 -c "print(id(None))"` даёт ровно это число).
Вывод был построен на диагностике, печатавшей id отсутствующего атрибута.
Число выглядело правдоподобно — ровно та цена, за которую в AGENTS.md
записано правило «исполни, не вспоминай»: правдоподобное и верное здесь
различает только повторный замер.

Класс уже был описан в шапке `test_mechanical_rebase.py` и решён там для
одного файла; здесь он закрыт для всех.

## Почему гвардия, а не «починим тесты»

Починка оставшихся семнадцати утечек — работа на несколько заходов, а класс
надо закрыть сегодня: НОВЫЙ тест не должен иметь возможности уйти в сеть молча.
Поэтому список `ALLOWED_LIVE_GH` фиксирует ровно нынешний долг, с причиной
на запись, и может только СОКРАЩАТЬСЯ — тест, которого в нём нет, падает
громко.

## Газ

Запись снимается из списка вместе с починкой теста. Обратное добавление —
осознанное решение с причиной в этом же файле, а не умолчание.

Два тормоза против «список тихо растёт / переживает переименования»
(замечание ai-review PR #1447):

* потолок `MAX_KNOWN_LIVE_GH_DEBT` — запись СВЕРХ потолка красит мета-тест;
  газ: поднять число здесь же, одной строкой, осознанно — тем самым размер
  долга меняется только заметным действием, а не довеском «ещё одного
  тестика» в общем PR;
* каждая запись сверяется с живыми `def test_*` каталога `scripts/orchestra`:
  запись, чей тест переименовали или удалили, красит мета-тест — мёртвая
  запись не может молча освобождать однофамильца.

## Чего этот файл НЕ делает, и почему — по замеру, а не по осторожности

Напрашивающаяся «общая» починка — патчить `gh` не по рукописному списку
модулей (`patch_gh` в test_scheduler.py знает три: scheduler, pulse_guard,
stall_detector), а обнаружением: у КАЖДОГО загруженного модуля репозитория,
у которого есть свой `gh`. Я это реализовал и прогнал: **277 failed,
153 passed** против 430 passed на базе.

То есть рукописный список здесь НЕСУЩИЙ, а не забытый: у остальных модулей
`gh` участвует в сценариях, которые стенд `FakeGh` не покрывает, и подмена
меняет их поведение. Общая замена — не «доведение до конца», а поломка.

Вывод, за который заплачено прогоном: утечки чинятся ПОШТУЧНО, с разбором
того, какой именно объект зовёт прод-код в каждом случае. Две уже починены
именно так (точечный патч экземпляра `pulse_guard`, который держит
`upstream_drift`) — значит путь рабочий, просто не оптовый. Этот
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
    "утечка патча: прод-код зовёт gh у НЕ ТОГО экземпляра модуля (#1438) — "
    "чинится патчем того объекта, который реально зовёт прод-код; запись "
    "снимается вместе с починкой, как уже сняты две сторожевые"
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
        "test_new_stale_episode_alerts_and_carries_the_marker",
        "test_continuing_stale_episode_neither_alerts_nor_reddens",
        "test_main_runs_the_catch_up_pass_every_pulse",
        "test_main_reddens_when_the_catch_up_pass_itself_is_broken",
    )
}

# Замеренный размер долга на 2026-09-22 (замер через Popen, см. шапку).
# Мета-тест красит список, выросший сверх этого числа: довесок записи —
# осознанное решение, а не побочный эффект чужого PR. Газ — поднять число
# здесь же с причиной в комментарии к изменению.
MAX_KNOWN_LIVE_GH_DEBT = 17

# Точка подмены — Popen, а не run (находка ai-review PR #1447).
# `subprocess.run` — лишь одна из форм запуска: `check_output`, `check_call`,
# `call`, `getoutput` CPython строит НАПРЯМУЮ через Popen, минуя run. Пока
# гвардия патчила только run, новый тест с `subprocess.check_output(["gh", …])`
# уходил бы в сеть молча — под вывеской «живой gh невозможен». Popen —
# единственное место, через которое проходят все формы сразу.
#
# Тем же замечанием опровергнута и полнота прошлого замера: он считал вызовы
# той же подменой `run`, то есть имел ровно это слепое пятно. Список ниже
# пересобран замером через Popen.
_REAL_POPEN = subprocess.Popen

_SHELL_WRAPPERS = ("bash", "sh", "zsh", "dash", "/bin/bash", "/bin/sh")


def _command_is_gh(cmd) -> bool:
    """Команда запускает `gh` — включая обёртку оболочкой.

    Разбираются три формы: список/кортеж, строка и обёртка
    `bash -c "gh api …"` (находка ai-review PR #1447: без неё обходной путь
    открыт в один шаг). Незнакомая форма считается НЕ gh — мишень гвардии
    узкая намеренно, ложное срабатывание на чужом запуске стоило бы того,
    что её выключат."""
    try:
        if isinstance(cmd, (list, tuple)):
            parts = [str(x) for x in cmd]
        elif isinstance(cmd, str):
            parts = cmd.split()
        else:
            return False
    except Exception:
        return False
    if not parts:
        return False

    first = parts[0].strip("\"'")
    if first == "gh" or first.endswith("/gh"):
        return True
    # bash -c "…gh api…" / sh -c "…": искомое слово внутри строки-скрипта.
    # Кавычки вокруг 'gh' снимает strip ниже: bash -c "'gh' api …" — это
    # ровно тот же живой вызов (оболочка сама снимает кавычки), и гвардия
    # обязана видеть его тем же глазом (замечание ai-review PR #1447).
    if first in _SHELL_WRAPPERS and "-c" in parts:
        script = " ".join(parts[parts.index("-c") + 1:])
        for token in script.replace(";", " ").replace("|", " ").replace("&", " ").split():
            bare = token.strip("\"'")
            if bare == "gh" or bare.endswith("/gh"):
                return True
    return False


@pytest.fixture(autouse=True)
def forbid_live_gh(request, monkeypatch):
    """Любой запуск `gh` из теста (`run`, `check_output`, `call` — все формы
    идут через Popen) — громкая ошибка.

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
                "Обычная причина — патч утёк мимо прод-кода: в одном прогоне живут "
                "ДВА экземпляра одного модуля, и прод-код зовёт не тот, который "
                "пропатчен (#1438; замеренный случай — pulse_guard у upstream_drift). "
                "Патчить надо тот объект, который реально зовёт прод-код.\n"
                "Осознанное исключение — запись в ALLOWED_LIVE_GH с причиной."
            )
        return _REAL_POPEN(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)
